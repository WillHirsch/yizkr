"""The exit statuses of every `yizkr` command.

One scheme for the whole CLI, so a script can tell what an invocation
came to from its status alone:

* EXIT_OK: it finished, and nothing failed.
* EXIT_FAILED: something failed: a document of a run, or the command
  itself on an error it does not anticipate (which prints a traceback).
* EXIT_STOPPED: it stopped short, and invoking it again goes on once the
  reason is dealt with: a pass paused by the engine or its cache
  (yizkr.pipeline.llm.PassPaused), a run's control file (`scratch run`;
  for `ingest run`, a stage it held), or, for `ingest run`, a document
  awaiting a review gate.
* EXIT_REFUSED: it refused before doing what it was asked, and printed
  why: a usage error, an input it will not act on, or a run directory
  that records other settings (yizkr.pipeline.rundir.RunSettingsConflict).
  Invoking it again unchanged is refused again.
* EXIT_INTERRUPTED: Ctrl-C.

The scratch commands give every refusal but a usage error EXIT_FAILED,
as they do a failure: their own reasons (`sys.exit`), their driver's
(yizkr.pipeline.scratch.ScratchRefused, RunSettingsConflict),
`scratch import-transcriptions`' ImportRefused
(yizkr.pipeline.import_transcriptions) and `scratch reset`'s ResetRefused
(yizkr.reset). `scratch estimate` and `scratch import-transcriptions` give
a pause EXIT_FAILED too.
"""

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_STOPPED = 2
EXIT_REFUSED = 3
EXIT_INTERRUPTED = 130
