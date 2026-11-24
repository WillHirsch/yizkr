"""An in-memory Wikibase and query service behind the real HTTP clients.

`FakeWikibase` answers the MediaWiki and Wikibase API requests that the
production `WikiClient`, WikibaseIntegrator and the query reader send, at
the transport: `serving` routes requests for its URLs to it inside the
process, so every request the code under test makes is one request here,
and `RequestLog` counts them by kind as they arrive. No production method
is replaced. `observing` counts the same kinds on a real stack's traffic
without answering it, so a fictional-lane run can be compared request for
request with an offline one.

The fake models what a measured run needs and refuses any other request
loudly (`UnsupportedRequest`), so a request it does not model cannot pass
unnoticed:

* entities: creation and edits through ``wbeditentity`` with Wikibase's
  merge of labels, descriptions, aliases and statements, statement GUIDs,
  snak and reference hashes, revisions, and an edit-conflict refusal of a
  stale base revision (a real instance can merge some stale edits; a single
  writer never sends one);
* pages: create-only reservations, page text, page listings by prefix,
  revision lookups, the newest revision and log entry, and no deletions or
  moves;
* uploads and file listings by SHA-1;
* the server mutation guard's inspect, begin, status and end, with every
  write refused without the active capability while an owner holds it;
* the query service: an index that follows the wiki only when
  `FakeQueryService.synchronize` is called, as the updater would after a
  lag, with the revision index and the roster computed from the indexed
  entities. Birth, death and containment queries return no rows, and the
  unsupported-date query returns none for the Gregorian dates the
  generated cohorts hold: their requests are counted, their rows are not
  modelled.
"""

from __future__ import annotations

import copy
import email.message
import email.parser
import email.policy
import hashlib
import io
import json
import re
import sys
import urllib.parse
import urllib.request
import urllib.response
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter

from yizkr import died_by
from yizkr.backup import REVISIONS_QUERY
from yizkr.core.config import Config
from yizkr.core.dates import GREGORIAN, UNSUPPORTED_QUERY
from yizkr.core.write_recovery import PREFIX
from yizkr.model.schema import load_manifest
from yizkr.pipeline.reconcile import ROSTER_QUERY
from yizkr.queries.publish import load_templates

WIKI_URL = "http://wikibase.fictional.invalid"
QUERY_URL = "http://query.fictional.invalid/sparql"
ADMIN = "FictionalAdmin"
PASSWORD = "fictional-offline-only"
# MediaWiki's namespaces for the project, files, items and properties, as
# the fictional lane's Wikibase Suite numbers them
PROJECT, FILE, ITEM, PROPERTY = 4, 6, 120, 122
_NAMESPACES = {"Project": PROJECT, "File": FILE, "Item": ITEM}
_NAMESPACES["Property"] = PROPERTY
_ALLPAGES_LIMIT = 500
_ALLREVISIONS_LIMIT = 500
_GUARD = "yizkr-mutation-guard-v1"
_RDF_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"


class UnsupportedRequest(AssertionError):
    """A request the fake does not model: a measured run must not send it,
    or the fake must learn it before its counts can be trusted."""


class ApiError(Exception):
    """A MediaWiki API error answer, as the server words one."""

    def __init__(self, code: str, info: str) -> None:
        super().__init__(code)
        self.code, self.info = code, info


def config(*, url: str = WIKI_URL, query_url: str = QUERY_URL) -> Config:
    """Connection settings that name the fake and no archive instance."""
    return Config(
        wikibase_url=url,
        concept_uri=url,
        admin_name=ADMIN,
        admin_pass=PASSWORD,
        wdqs_url=query_url.rsplit("/", 1)[0],
        sparql_url=query_url,
        env_file=None,
    )


# -- request kinds ------------------------------------------------------------


def _select_line(query: str) -> str:
    return next(
        line.strip()
        for line in query.splitlines()
        if line.lstrip().startswith("SELECT")
    )


def _query_purposes() -> dict[str, str]:
    templates = load_templates()
    return {
        _select_line(REVISIONS_QUERY): "revision-index",
        _select_line(UNSUPPORTED_QUERY): "unsupported-dates",
        _select_line(ROSTER_QUERY): "roster",
        _select_line(templates["birth-evidence"]): "births",
        _select_line(templates[died_by.QUERY]): "deaths",
        _select_line(templates["place-containment"]): "containment",
    }


_PURPOSES = _query_purposes()


def query_purpose(query: str) -> str:
    """What a query-service query reads, by its SELECT line; "other" for a
    query no measured reader sends."""
    try:
        return _PURPOSES.get(_select_line(query), "other")
    except StopIteration:
        return "other"


def request_kind(params: Mapping[str, str]) -> str:
    """The kind of one MediaWiki API request, by its parameters: what the
    client method that sends it reads or writes. A whole-instance entity
    enumeration starts with "entity-scan" (its first listing request);
    its further listing pages and entity batches have kinds of their own.
    An immutable page read is two "page-read" requests."""
    action = params.get("action")
    if action == "query":
        listing = params.get("list")
        if listing == "allpages":
            if params.get("apnamespace") == str(PROJECT):
                return "page-list"
            if params.get("apnamespace") == "0" and "apcontinue" not in params:
                return "entity-scan"
            return "entity-scan-page"
        if listing == "logevents":
            return "page-read" if "letitle" in params else "watermark"
        if listing == "allrevisions":
            if "comment" in params.get("arvprop", ""):
                return "creation-scan"
            return "watermark"
        if listing == "allimages":
            return "file-read"
        if params.get("meta") == "tokens":
            return "login"
        if params.get("meta") == "siteinfo":
            return "site-info"
        if params.get("prop") == "revisions":
            return "creation-check" if "revids" in params else "page-text"
        if params.get("prop") == "imageinfo":
            return "file-read"
        if params.get("prop") == "info":
            return "kept-marker"
    if action == "wbgetentities":
        props = params.get("props")
        if props is None:
            return "item-read"
        if props == "info":
            if params.get("redirects") == "no":
                return "creation-check"
            return "revision-read"
        if "languages" in params and props in ("claims", "labels|claims"):
            return "entity-scan-batch"
        if props == "labels|datatype":
            return "label-index"
        if props == "labels":
            return "vocabulary-read"
        if props == "claims":
            return "statement-read"
        return "entity-read"
    if action == "edit":
        return "page-write"
    if action == "wbeditentity":
        return "item-create" if "new" in params else "item-edit"
    if action == "upload":
        return "upload"
    if action == "yizkrmutationguard":
        return "guard"
    if action == "clientlogin":
        return "login"
    return f"other:{action}"


def record_kind(title: str) -> str:
    """The kind of registry record a page title holds, by its locator, as
    the creation registry and alias records name them."""
    if not title.startswith(PREFIX):
        return "other-page"
    rest = title.removeprefix(PREFIX)
    if rest == "generation":
        return "generation"
    _, _, local = rest.partition("/")
    patterns = (
        (r"[0-9a-f]{64}/(intent|dispatch|receipt)", "creation-{0}"),
        (r"entities/[0-9a-f]{32}/binding/1", "binding-index"),
        (r"reviewed-creations/[0-9a-f]{32}/authorization", "authorization"),
        (r"assignments/[^/]+/next", "assignment-commit"),
        (r"assignment-aliases/[^/]+/(\w+)", "alias-{0}"),
        (r"applications/[0-9a-f]{64}/plan", "application-plan"),
        (
            r"applications/[0-9a-f]{64}/[0-9a-f]{64}/(intent|receipt)",
            "application-{0}",
        ),
        (r"support-dispatch/Q\d+/\d+", "support-dispatch"),
        (r"identity-map", "identity-map"),
        (r"migrations/[0-9a-f]{64}/receipt", "migration-receipt"),
    )
    for pattern, kind in patterns:
        match = re.fullmatch(pattern, local)
        if match:
            return kind.format(*match.groups())
    return "other-record"


def _sender() -> tuple[str, ...]:
    """The yizkr functions on the stack, outermost first, without the wiki
    client, whose methods only carry a request."""
    names = []
    frame = sys._getframe(2)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module.startswith("yizkr.") and module != "yizkr.core.wiki":
            names.append(
                f"{module.removeprefix('yizkr.')}.{frame.f_code.co_qualname}"
            )
        frame = frame.f_back
    return tuple(reversed(names))


def account(log: RequestLog, params: Mapping[str, str]) -> None:
    """Count one API request in `log` by kind, and a write request by what
    it writes: "page:<record kind>" for a page edit, "item-create" and
    "item-update" for an item, "file" for an upload. A write is counted as
    it is sent, so a refused one counts too; a single writer sends none
    that is refused."""
    kind = request_kind(params)
    log.request(kind)
    if kind == "page-write":
        log.wrote(f"page:{record_kind(params.get('title', ''))}")
    elif kind == "item-create":
        log.wrote("item-create")
    elif kind == "item-edit":
        log.wrote("item-update")
    elif kind == "upload":
        log.wrote("file")


@dataclass
class RequestLog:
    """Requests by phase and kind, and what the server wrote by phase and
    kind: `requests[phase][kind]` and `writes[phase][kind]`. The phase is
    the one `phase` last opened; query-service queries are counted as
    "query:<purpose>"."""

    current: str = "setup"
    requests: dict[str, Counter] = field(default_factory=dict)
    writes: dict[str, Counter] = field(default_factory=dict)
    # with `trace`, every request is also counted by its phase, kind and
    # the yizkr functions that sent it, outermost first: `paths`
    trace: bool = False
    paths: Counter = field(default_factory=Counter)

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        previous, self.current = self.current, name
        try:
            yield
        finally:
            self.current = previous

    def request(self, kind: str, count: int = 1) -> None:
        self.requests.setdefault(self.current, Counter())[kind] += count
        if self.trace:
            self.paths[self.current, kind, _sender()] += count

    def wrote(self, kind: str) -> None:
        self.writes.setdefault(self.current, Counter())[kind] += 1

    def totals(self) -> tuple[Counter, Counter]:
        """(requests by kind, writes by kind) over every phase."""
        requests, writes = Counter(), Counter()
        for counts in self.requests.values():
            requests.update(counts)
        for counts in self.writes.values():
            writes.update(counts)
        return requests, writes


# -- transport ----------------------------------------------------------------


def _form(body) -> dict[str, str]:
    if isinstance(body, bytes):
        body = body.decode()
    return dict(urllib.parse.parse_qsl(body or "", keep_blank_values=True))


def _multipart(body: bytes, content_type: str) -> tuple[dict, dict]:
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body
    )
    fields, files = {}, {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            files[name] = payload
        else:
            fields[name] = payload.decode()
    return fields, files


def request_parts(prepared) -> tuple[str, dict[str, str], dict, dict]:
    """(method, parameters, files, headers) of a prepared requests call:
    the URL's query and the form or multipart body together."""
    url = urllib.parse.urlsplit(prepared.url)
    params = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
    files = {}
    content_type = prepared.headers.get("Content-Type", "")
    if content_type.startswith("multipart/form-data"):
        fields, files = _multipart(prepared.body, content_type)
        params.update(fields)
    elif prepared.body:
        params.update(_form(prepared.body))
    return prepared.method, params, files, dict(prepared.headers)


def _sparql_query(data) -> str:
    return _form(data).get("query", "")


def _text(value) -> str:
    return value if isinstance(value, str) else str(value)


def _uploaded(files) -> dict[str, bytes]:
    held = {}
    for name, value in (files or {}).items():
        content = value[1] if isinstance(value, tuple) else value
        held[name] = content.read() if hasattr(content, "read") else content
    return held


def _response(url: str, payload: dict) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Type"] = "application/json"
    response.raw = io.BytesIO(json.dumps(payload).encode())
    response.url = url
    response.encoding = "utf-8"
    return response


class _QueryHandler(urllib.request.BaseHandler):
    handler_order = 100

    def __init__(self, service: FakeQueryService, url: str) -> None:
        self.service, self.url = service, url

    def http_open(self, request):
        if request.full_url != self.url:
            return None
        rows = self.service.answer(_sparql_query(request.data))
        body = json.dumps({"results": {"bindings": rows}}).encode()
        headers = email.message.Message()
        headers["Content-Type"] = "application/sparql-results+json"
        response = urllib.response.addinfourl(
            io.BytesIO(body), headers, request.full_url, 200
        )
        response.msg = "OK"
        return response


class _QueryCounter(urllib.request.BaseHandler):
    handler_order = 100

    def __init__(self, log: RequestLog, url: str) -> None:
        self.log, self.url = log, url

    def http_request(self, request):
        if request.full_url == self.url:
            query = _sparql_query(request.data)
            self.log.request(f"query:{query_purpose(query)}")
        return request

    https_request = http_request


@contextmanager
def _opener(handler) -> Iterator[None]:
    previous = urllib.request._opener
    urllib.request.install_opener(urllib.request.build_opener(handler))
    try:
        yield
    finally:
        urllib.request.install_opener(previous)


@contextmanager
def serving(wiki: FakeWikibase) -> Iterator[FakeWikibase]:
    """Route this process's requests for the fake's API and query URLs to
    it while the block runs; every other URL is left as it is. A request
    reaches the fake as the requests library's session receives it, with
    its parameters, form, files and headers, before the library prepares
    it for the network."""
    original = requests.Session.request

    def request(session, method, url, params=None, data=None, **kwargs):
        if not url.startswith(wiki.url + "/"):
            return original(
                session, method, url, params=params, data=data, **kwargs
            )
        sent = {
            key: _text(value)
            for given in (params or {}, data or {})
            for key, value in given.items()
        }
        headers = {**session.headers, **(kwargs.get("headers") or {})}
        payload = wiki.api(
            method, sent, _uploaded(kwargs.get("files")), headers
        )
        return _response(url, payload)

    requests.Session.request = request
    try:
        with _opener(_QueryHandler(wiki.query_service, wiki.query_url)):
            yield wiki
    finally:
        requests.Session.request = original


@contextmanager
def observing(log: RequestLog, settings: Config) -> Iterator[RequestLog]:
    """Count this process's requests to the instance `settings` names by
    kind in `log`, as `serving` counts the fake's, and pass them on."""
    original = HTTPAdapter.send
    api = settings.wikibase_url + "/w/api.php"

    def send(adapter, request, **kwargs):
        if request.url.split("?", 1)[0] == api:
            _, params, _, _ = request_parts(request)
            account(log, params)
        return original(adapter, request, **kwargs)

    HTTPAdapter.send = send
    try:
        with _opener(_QueryCounter(log, settings.sparql_url)):
            yield log
    finally:
        HTTPAdapter.send = original


# -- the wiki -----------------------------------------------------------------


def _hash(value) -> str:
    return hashlib.sha1(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _local(title: str) -> tuple[int, str]:
    namespace, separator, local = title.partition(":")
    if separator and namespace in _NAMESPACES:
        return _NAMESPACES[namespace], local
    return 0, title


def _entity_title(entity_id: str) -> str:
    return ("Property:" if entity_id.startswith("P") else "Item:") + entity_id


def _file_title(title: str) -> str:
    namespace, _, name = title.partition(":")
    name = name.replace("_", " ").strip()
    return f"File:{name[:1].upper()}{name[1:]}"


@dataclass
class _Revision:
    revid: int
    parentid: int
    title: str
    comment: str
    text: str | None = None


class FakeWikibase:
    """One Wikibase instance's API, held in memory. Construct it, then
    `bootstrap` its vocabulary through the real bootstrap, inside
    `serving`."""

    def __init__(self, *, url: str = WIKI_URL, query_url: str = QUERY_URL):
        self.url, self.query_url = url, query_url
        self.api_url = url + "/w/api.php"
        self.log = RequestLog()
        self.entities: dict[str, dict] = {}
        self.entity_pages: dict[str, list[int]] = {}
        self.pages: dict[str, list[int]] = {}
        self.revisions: dict[int, _Revision] = {}
        self.files: dict[str, str] = {}
        self.logs = 0
        self._next = {"Q": 1, "P": 1}
        self._statements = 0
        self.guard = {
            "protocol": _GUARD,
            "instance": hashlib.md5(url.encode()).hexdigest(),
            "epoch": 0,
            "active": False,
            "operation_id": None,
        }
        self._capability: str | None = None
        self.query_service = FakeQueryService(self)

    # -- dispatch --------------------------------------------------------

    def api(self, method, params, files, headers) -> dict:
        account(self.log, params)
        try:
            return self._answer(method, params, files, headers)
        except ApiError as error:
            return {"error": {"code": error.code, "info": error.info}}

    def _answer(self, method, params, files, headers) -> dict:
        action = params.get("action")
        if action in ("edit", "wbeditentity", "upload"):
            self._admit_write(headers)
        if action == "query":
            return self._query(params)
        if action == "wbgetentities":
            return self._get_entities(params)
        if action == "wbeditentity":
            return self._edit_entity(params)
        if action == "edit":
            return self._edit_page(params)
        if action == "upload":
            return self._upload(params, files)
        if action == "yizkrmutationguard":
            return self._mutation_guard(params, headers)
        if action == "clientlogin":
            if (params.get("username"), params.get("password")) != (
                ADMIN,
                PASSWORD,
            ):
                raise ApiError("login-failed", "fictional login refused")
            return {"clientlogin": {"status": "PASS", "username": ADMIN}}
        raise UnsupportedRequest(f"unmodelled API request: {params}")

    def _admit_write(self, headers) -> None:
        capability = headers.get("X-Yizkr-Mutation-Capability")
        if self.guard["active"] and capability != self._capability:
            raise ApiError(
                "yizkr-mutation-excluded",
                "another operation holds the mutation guard",
            )

    # -- revisions and pages ---------------------------------------------

    def _revise(self, title: str, comment: str, text=None) -> _Revision:
        history = self.pages.setdefault(title, [])
        revid = len(self.revisions) + 1
        revision = _Revision(
            revid=revid,
            parentid=history[-1] if history else 0,
            title=title,
            comment=comment,
            text=text,
        )
        self.revisions[revid] = revision
        if not history:
            self.logs += 1  # a page creation adds a creation log entry
        history.append(revid)
        return revision

    def page(self, title: str) -> _Revision | None:
        history = self.pages.get(title)
        return self.revisions[history[-1]] if history else None

    def _page_json(self, title, content: bool, version2: bool) -> dict:
        namespace, _ = _local(title)
        revision = self.page(title)
        if revision is None:
            return {
                "ns": namespace,
                "title": title,
                "missing": True if version2 else "",
            }
        found = {
            "pageid": revision.revid,
            "ns": namespace,
            "title": title,
            "revisions": [
                {
                    "revid": revision.revid,
                    "parentid": revision.parentid,
                    **(
                        {
                            "slots": {
                                "main": {
                                    "contentmodel": "wikitext",
                                    "contentformat": "text/x-wiki",
                                    "content": revision.text,
                                }
                            }
                        }
                        if content
                        else {}
                    ),
                }
            ],
        }
        return found

    def _edit_page(self, params) -> dict:
        title = params["title"]
        if params.get("createonly") and title in self.pages:
            raise ApiError(
                "articleexists", "The article you tried to create exists"
            )
        old = self.page(title)
        revision = self._revise(
            title, params.get("summary", ""), params["text"]
        )
        return {
            "edit": {
                "result": "Success",
                "title": title,
                "oldrevid": old.revid if old else 0,
                "newrevid": revision.revid,
            }
        }

    # -- queries ---------------------------------------------------------

    def _query(self, params) -> dict:
        version2 = params.get("formatversion") == "2"
        listing = params.get("list")
        if params.get("meta") == "tokens":
            kind = params.get("type", "csrf")
            return {"query": {"tokens": {f"{kind}token": "fictional+\\"}}}
        if params.get("meta") == "siteinfo":
            return {"query": {"general": {"sitename": "Fictional offline"}}}
        if listing == "allpages":
            return self._allpages(params)
        if listing == "logevents":
            if "letitle" not in params:
                return {
                    "query": {
                        "logevents": [{"logid": self.logs}]
                        if self.logs
                        else []
                    }
                }
            # no page is ever deleted or moved here
            answer = {"query": {"logevents": []}}
            if params.get("prop") == "revisions":
                answer["query"]["pages"] = [
                    self._page_json(params["titles"], True, version2)
                ]
            return answer
        if listing == "allrevisions":
            return self._allrevisions(params)
        if listing == "allimages":
            sha1 = params["aisha1"]
            return {
                "query": {
                    "allimages": [
                        {"name": name, "title": f"File:{name}"}
                        for name, held in sorted(self.files.items())
                        if held == sha1
                    ]
                }
            }
        prop = params.get("prop")
        if prop == "revisions" and "revids" in params:
            revision = self.revisions.get(int(params["revids"]))
            if revision is None:
                return {"query": {"badrevids": {params["revids"]: {}}}}
            return {
                "query": {
                    "pages": [
                        {
                            "title": revision.title,
                            "revisions": [
                                {
                                    "revid": revision.revid,
                                    "parentid": revision.parentid,
                                    "comment": revision.comment,
                                }
                            ],
                        }
                    ]
                }
            }
        if prop == "revisions" and "titles" in params:
            return {
                "query": {
                    "pages": [
                        self._page_json(params["titles"], True, version2)
                    ]
                }
            }
        if prop == "info" and "titles" in params:
            pages = {}
            for index, title in enumerate(params["titles"].split("|"), 1):
                page = self._page_json(title, False, False)
                page.pop("revisions", None)
                pages[str(page.get("pageid", -index))] = page
            return {"query": {"pages": pages}}
        if prop == "imageinfo":
            return self._imageinfo(params["titles"].split("|"))
        raise UnsupportedRequest(f"unmodelled query: {params}")

    def _allpages(self, params) -> dict:
        namespace = int(params["apnamespace"])
        prefix = params.get("apprefix", "")
        if namespace in (ITEM, PROPERTY):
            letter = "Q" if namespace == ITEM else "P"
            titles = [
                _entity_title(entity)
                for entity in self.entities
                if entity.startswith(letter)
            ]
        elif namespace == PROJECT:
            titles = [
                title for title in self.pages if title.startswith("Project:")
            ]
        elif namespace == 0:
            titles = []
        else:
            raise UnsupportedRequest(f"unmodelled page listing: {params}")
        locals_ = sorted(
            local
            for local in (title.partition(":")[2] for title in titles)
            if local.startswith(prefix)
        )
        start = params.get("apcontinue")
        if start is not None:
            locals_ = [local for local in locals_ if local >= start]
        limit = int(params.get("aplimit", 10))
        shown, rest = locals_[:limit], locals_[limit:]
        name = {
            ITEM: "Item",
            PROPERTY: "Property",
            PROJECT: "Project",
        }.get(namespace)
        answer = {
            "query": {
                "allpages": [
                    {"ns": namespace, "title": f"{name}:{local}"}
                    for local in shown
                ]
            }
        }
        if rest:
            answer["continue"] = {"apcontinue": rest[0], "continue": "-||"}
        return answer

    def _allrevisions(self, params) -> dict:
        # revision ids are consecutive from 1, newest last
        limit = int(params.get("arvlimit", 1))
        start = int(params.get("arvcontinue", len(self.revisions)))
        shown = list(range(start, max(start - limit, 0), -1))
        rest = [start - limit] if start - limit > 0 else []
        with_comment = "comment" in params.get("arvprop", "")
        pages: dict[str, list] = {}
        for revid in shown:
            revision = self.revisions[revid]
            pages.setdefault(revision.title, []).append(
                {
                    "revid": revid,
                    "parentid": revision.parentid,
                    **({"comment": revision.comment} if with_comment else {}),
                }
            )
        answer = {
            "query": {
                "allrevisions": [
                    {"title": title, "revisions": found}
                    for title, found in pages.items()
                ]
            }
        }
        if rest:
            answer["continue"] = {
                "arvcontinue": str(rest[0]),
                "continue": "-||",
            }
        return answer

    def _imageinfo(self, titles) -> dict:
        normalized, pages = [], {}
        for index, title in enumerate(titles, 1):
            canonical = _file_title(title)
            if canonical != title:
                normalized.append({"from": title, "to": canonical})
            name = canonical.removeprefix("File:")
            if name in self.files:
                pages[str(index)] = {
                    "title": canonical,
                    "imageinfo": [{"sha1": self.files[name]}],
                }
            else:
                pages[str(-index)] = {"title": canonical, "missing": ""}
        return {"query": {"normalized": normalized, "pages": pages}}

    def _upload(self, params, files) -> dict:
        data = files.get("file")
        if data is None:
            raise UnsupportedRequest("upload without a file")
        sha1 = hashlib.sha1(data).hexdigest()
        name = _file_title(f"File:{params['filename']}").removeprefix("File:")
        holders = sorted(n for n, held in self.files.items() if held == sha1)
        if not params.get("ignorewarnings"):
            if name in self.files and self.files[name] == sha1:
                return {
                    "upload": {
                        "result": "Warning",
                        "warnings": {"exists": name, "nochange": {}},
                    }
                }
            if holders:
                return {
                    "upload": {
                        "result": "Warning",
                        "warnings": {"duplicate": holders},
                    }
                }
            if name in self.files:
                return {
                    "upload": {
                        "result": "Warning",
                        "warnings": {"exists": name},
                    }
                }
        self.files[name] = sha1
        self._revise(f"File:{name}", params.get("comment", ""), "")
        self.logs += 1
        return {"upload": {"result": "Success", "filename": name}}

    # -- the mutation guard ----------------------------------------------

    def _mutation_guard(self, params, headers) -> dict:
        mode = params["mode"]
        state = self.guard
        capability = headers.get("X-Yizkr-Mutation-Capability")
        if mode == "begin":
            parts = (capability or "").split(".")
            if len(parts) != 3 or parts[0] != state["instance"]:
                raise ApiError("yizkr-mutation-guard", "invalid capability")
            if state["active"]:
                if capability != self._capability:
                    raise ApiError("yizkr-mutation-guard", "guard is held")
            elif int(parts[1]) != state["epoch"] + 1:
                raise ApiError("yizkr-mutation-guard", "stale epoch")
            else:
                state.update(
                    epoch=int(parts[1]),
                    active=True,
                    operation_id=params["operation"],
                )
                self._capability = capability
        elif mode == "end":
            if state["active"] and capability == self._capability:
                state.update(active=False, operation_id=None)
                self._capability = None
        elif mode not in ("inspect", "status"):
            raise UnsupportedRequest(f"unmodelled guard mode: {mode}")
        return {"yizkrmutationguard": dict(state)}

    # -- entities --------------------------------------------------------

    def datatype(self, pid: str) -> str:
        return self.entities[pid]["datatype"]

    def revision_of(self, entity_id: str) -> int:
        return self.entity_pages[entity_id][-1]

    def entity_json(self, entity_id: str, props=None, languages=None):
        """The entity as wbgetentities serves it. Its parts are the stored
        ones, which an edit replaces and never changes in place, so the
        answer, serialized at once, needs no copy."""
        stored = self.entities[entity_id]
        title = _entity_title(entity_id)
        revision = self.revision_of(entity_id)
        entity = {"type": stored["type"], "id": entity_id}
        wanted = (
            {"info", "labels", "descriptions", "aliases", "claims"}
            | {"sitelinks", "datatype"}
            if props is None
            else set(props.split("|"))
        )
        if "info" in wanted:
            entity.update(
                pageid=self.entity_pages[entity_id][0],
                ns=PROPERTY if entity_id.startswith("P") else ITEM,
                title=title,
                lastrevid=revision,
                modified="2026-01-01T00:00:00Z",
            )
        for key in ("labels", "descriptions", "aliases"):
            if key in wanted:
                terms = stored[key]
                if languages is not None:
                    terms = {
                        language: value
                        for language, value in terms.items()
                        if language in languages
                    }
                entity[key] = terms
        if "claims" in wanted:
            entity["claims"] = stored["claims"]
        if "sitelinks" in wanted and stored["type"] == "item":
            entity["sitelinks"] = {}
        if "datatype" in wanted and stored["type"] == "property":
            entity["datatype"] = stored["datatype"]
        return entity

    def _get_entities(self, params) -> dict:
        languages = (
            set(params["languages"].split("|"))
            if "languages" in params
            else None
        )
        entities = {}
        for entity_id in params["ids"].split("|"):
            if entity_id in self.entities:
                entities[entity_id] = self.entity_json(
                    entity_id, params.get("props"), languages
                )
            else:
                entities[entity_id] = {"id": entity_id, "missing": ""}
        return {"entities": entities, "success": 1}

    def _snak(self, snak: dict) -> dict:
        pid = snak["property"]
        if pid not in self.entities:
            raise ApiError("no-such-property", f"unknown property {pid}")
        core = {"snaktype": snak["snaktype"], "property": pid}
        if "datavalue" in snak:
            core["datavalue"] = copy.deepcopy(snak["datavalue"])
            value = core["datavalue"].get("value")
            if isinstance(value, str):
                core["datavalue"]["value"] = value.strip()
        return {**core, "hash": _hash(core), "datatype": self.datatype(pid)}

    def _snaks(self, snaks: dict, order) -> tuple[dict, list]:
        held = {
            pid: [self._snak(snak) for snak in listed]
            for pid, listed in snaks.items()
        }
        order = [pid for pid in (order or held) if pid in held]
        return held, order

    def _statement(self, entity_id: str, statement: dict) -> dict:
        if statement.get("id") is None:
            self._statements += 1
            guid = str(uuid.UUID(int=self._statements)).upper()
            statement_id = f"{entity_id}${guid}"
        else:
            statement_id = statement["id"]
        stored = {
            "mainsnak": self._snak(statement["mainsnak"]),
            "type": "statement",
            "id": statement_id,
            "rank": statement.get("rank", "normal"),
        }
        if statement.get("qualifiers"):
            qualifiers, order = self._snaks(
                statement["qualifiers"], statement.get("qualifiers-order")
            )
            stored["qualifiers"] = qualifiers
            stored["qualifiers-order"] = order
        references = []
        for reference in statement.get("references", ()):
            snaks, order = self._snaks(
                reference["snaks"], reference.get("snaks-order")
            )
            core = [(pid, snaks[pid]) for pid in order]
            references.append(
                {"hash": _hash(core), "snaks": snaks, "snaks-order": order}
            )
        stored["references"] = references
        return stored

    def _merge(self, entity_id: str, stored: dict, data: dict) -> None:
        for key in ("labels", "descriptions"):
            for language, term in (data.get(key) or {}).items():
                value = (term.get("value") or "").strip()
                if "remove" in term or not value:
                    stored[key].pop(language, None)
                else:
                    stored[key][language] = {
                        "language": language,
                        "value": value,
                    }
        for language, aliases in (data.get("aliases") or {}).items():
            current = [
                alias["value"] for alias in stored["aliases"].get(language, [])
            ]
            plain = [
                alias["value"].strip()
                for alias in aliases
                if "remove" not in alias and "add" not in alias
            ]
            if plain:
                current = plain
            current += [
                alias["value"].strip() for alias in aliases if "add" in alias
            ]
            removed = {
                alias["value"].strip()
                for alias in aliases
                if "remove" in alias
            }
            kept = [
                value
                for value in dict.fromkeys(current)
                if value and value not in removed
            ]
            if kept:
                stored["aliases"][language] = [
                    {"language": language, "value": value} for value in kept
                ]
            else:
                stored["aliases"].pop(language, None)
        claims = data.get("claims") or {}
        if isinstance(claims, list):
            listed: dict[str, list] = {}
            for statement in claims:
                listed.setdefault(
                    statement["mainsnak"]["property"], []
                ).append(statement)
            claims = listed
        for pid, statements in claims.items():
            for statement in statements:
                held = stored["claims"].setdefault(pid, [])
                position = next(
                    (
                        index
                        for index, present in enumerate(held)
                        if statement.get("id")
                        and present["id"] == statement["id"]
                    ),
                    None,
                )
                if "remove" in statement:
                    if position is not None:
                        del held[position]
                elif statement.get("id") and position is None:
                    raise ApiError(
                        "no-such-claim", f"no statement {statement['id']}"
                    )
                elif position is not None:
                    held[position] = self._statement(entity_id, statement)
                else:
                    held.append(self._statement(entity_id, statement))
                if not held:
                    del stored["claims"][pid]

    def _edit_entity(self, params) -> dict:
        data = json.loads(params["data"])
        summary = params.get("summary", "")
        if "new" in params:
            kind = params["new"]
            letter = "P" if kind == "property" else "Q"
            entity_id = f"{letter}{self._next[letter]}"
            self._next[letter] += 1
            stored = {
                "type": kind,
                "labels": {},
                "descriptions": {},
                "aliases": {},
                "claims": {},
            }
            if kind == "property":
                stored["datatype"] = data["datatype"]
            self.entities[entity_id] = stored
            action = "create"
        else:
            entity_id = params["id"]
            if entity_id not in self.entities:
                raise ApiError("no-such-entity", f"no entity {entity_id}")
            base = params.get("baserevid")
            if base is not None and int(base) != self.revision_of(entity_id):
                raise ApiError("editconflict", "edit conflict")
            stored = copy.deepcopy(self.entities[entity_id])
            if params.get("clear"):
                stored.update(
                    labels={}, descriptions={}, aliases={}, claims={}
                )
            action = "update"
        self._merge(entity_id, stored, data)
        self.entities[entity_id] = stored
        revision = self._revise(
            _entity_title(entity_id),
            f"/* wbeditentity-{action}:0| */ {summary}".rstrip(),
        )
        self.entity_pages.setdefault(entity_id, []).append(revision.revid)
        return {"entity": self.entity_json(entity_id), "success": 1}

    # -- set-up ----------------------------------------------------------

    def bootstrap(self) -> None:
        """Apply the manifest through the production bootstrap, as the
        fictional lane's stacks are set up, logged as phase "setup"."""
        from yizkr.core.wiki import WikiClient
        from yizkr.model.bootstrap import bootstrap_instance

        bootstrap_instance(
            WikiClient(config(url=self.url, query_url=self.query_url)),
            load_manifest(),
            log=lambda *args, **kwargs: None,
        )
        self.query_service.synchronize()


# -- the query service --------------------------------------------------------


def _uri(concept: str, entity_id: str) -> dict:
    return {"type": "uri", "value": f"{concept}/entity/{entity_id}"}


def _literal(value: str) -> dict:
    return {"type": "literal", "value": value}


def _truthy(entity: dict, pid: str) -> list[dict]:
    """The values of `pid`'s best-ranked statements, as wdt: holds them."""
    statements = [
        statement
        for statement in entity.get("claims", {}).get(pid, ())
        if statement["rank"] != "deprecated"
        and statement["mainsnak"]["snaktype"] == "value"
    ]
    if any(statement["rank"] == "preferred" for statement in statements):
        statements = [s for s in statements if s["rank"] == "preferred"]
    return [statement["mainsnak"]["datavalue"] for statement in statements]


class FakeQueryService:
    """The query service over the wiki's entities as last indexed."""

    def __init__(self, wiki: FakeWikibase) -> None:
        self.wiki = wiki
        self.indexed: dict[str, tuple[int, dict]] = {}
        self.synchronizations = 0
        self.manifest = load_manifest()

    def synchronize(self) -> None:
        """Index every entity at its current revision, as the updater
        does once it has caught up."""
        # an edit replaces an entity's stored data, so the index can hold
        # the data it saw without a copy
        self.indexed = {
            entity_id: (self.wiki.revision_of(entity_id), data)
            for entity_id, data in self.wiki.entities.items()
        }
        self.synchronizations += 1

    def lagging(self) -> int:
        """How many entities the index holds at another revision, or not."""
        return sum(
            self.indexed.get(entity_id, (None,))[0]
            != self.wiki.revision_of(entity_id)
            for entity_id in self.wiki.entities
        )

    def answer(self, query: str) -> list[dict]:
        purpose = query_purpose(query)
        self.wiki.log.request(f"query:{purpose}")
        concept = self.wiki.url
        if purpose == "revision-index":
            return [
                {
                    "entity": _uri(concept, entity_id),
                    "version": {
                        "type": "literal",
                        "datatype": _RDF_INTEGER,
                        "value": str(revision),
                    },
                }
                for entity_id, (revision, _) in sorted(self.indexed.items())
            ]
        if purpose == "roster":
            return self._roster(concept)
        if purpose == "unsupported-dates":
            return self._unsupported(concept)
        if purpose in ("births", "deaths", "containment"):
            return []
        raise UnsupportedRequest(f"unmodelled query: {query[:200]}")

    def _label(self, entity_id: str) -> str:
        entity = self.indexed.get(entity_id, (0, {}))[1]
        return (entity.get("labels", {}).get("en") or {}).get(
            "value", entity_id
        )

    def _roster(self, concept: str) -> list[dict]:
        properties = {
            (data.get("labels", {}).get("en") or {}).get("value"): entity_id
            for entity_id, (_, data) in self.indexed.items()
            if entity_id.startswith("P")
        }

        def pid(key: str) -> str | None:
            return properties.get(self.manifest.properties[key].label)

        names = {
            "birthNames": "birth_name",
            "marriedNames": "married_name",
            "nicknames": "nickname",
            "officialNames": "official_name",
        }
        kin = {
            "spouse": "spouse",
            "father": "father",
            "mother": "mother",
            "child": "child",
            "sibling": "sibling",
        }
        rows = []
        for qid, (_, entity) in sorted(self.indexed.items()):
            if not qid.startswith("Q"):
                continue
            kinds = [
                value["value"]["id"]
                for value in _truthy(entity, pid("instance_of"))
            ]
            if not kinds:
                continue
            aliases = sorted(
                {alias["value"] for alias in entity["aliases"].get("en", [])}
            )
            row = {
                "item": _uri(concept, qid),
                "itemLabel": _literal(self._label(qid)),
                "aliases": _literal("|".join(aliases)),
            }
            for name, key in names.items():
                values = sorted(
                    {
                        value["value"]
                        if isinstance(value["value"], str)
                        else value["value"].get("text", "")
                        for value in _truthy(entity, pid(key))
                    }
                )
                row[name] = _literal("|".join(values))
            for name, key in kin.items():
                related = sorted(
                    {
                        value["value"]["id"]
                        for value in _truthy(entity, pid(key))
                    }
                )
                row[f"{name}Qids"] = _literal("|".join(related))
                if name in ("spouse", "father", "mother"):
                    row[f"{name}s" if name == "spouse" else f"{name}s"] = (
                        _literal(
                            "|".join(
                                f"{self._label(other)} ({other})"
                                for other in related
                            )
                        )
                    )
            appellations = sorted(
                {
                    statement["mainsnak"]["datavalue"]["value"]
                    if isinstance(
                        statement["mainsnak"]["datavalue"]["value"], str
                    )
                    else statement["mainsnak"]["datavalue"]["value"]["text"]
                    for statement in entity.get("claims", {}).get(
                        pid("referred_to_as"), ()
                    )
                    if statement["rank"] != "deprecated"
                    and "datavalue" in statement["mainsnak"]
                }
            )
            if appellations:
                row["appellations"] = _literal("|".join(appellations))
            for kind in sorted(set(kinds)):
                rows.append(
                    {
                        **row,
                        "kind": _uri(concept, kind),
                        "kindLabel": _literal(self._label(kind)),
                    }
                )
        return rows

    def _unsupported(self, concept: str) -> list[dict]:
        rows = []
        for entity_id, (_, entity) in sorted(self.indexed.items()):
            for statements in entity.get("claims", {}).values():
                for statement in statements:
                    snaks = [statement["mainsnak"]]
                    for listed in statement.get("qualifiers", {}).values():
                        snaks.extend(listed)
                    for reference in statement.get("references", ()):
                        for listed in reference["snaks"].values():
                            snaks.extend(listed)
                    for snak in snaks:
                        value = snak.get("datavalue", {})
                        if value.get("type") != "time":
                            continue
                        time = value["value"]["time"]
                        calendar = value["value"]["calendarmodel"]
                        if (
                            calendar != GREGORIAN
                            or time.startswith("-")
                            or time.lstrip("+").startswith("0000-")
                        ):
                            rows.append(
                                {
                                    "entity": _uri(concept, entity_id),
                                    "statement": _literal(statement["id"]),
                                    "time": _literal(time),
                                    "calendar": {
                                        "type": "uri",
                                        "value": calendar,
                                    },
                                }
                            )
        return rows
