"""The exit statuses of every `yizkr` command.

One scheme for the whole CLI, so a script can tell what an invocation
came to from its status alone:

* EXIT_OK: it finished, and nothing failed.
* EXIT_FAILED: something failed: a document of a run, or the command
  itself on an error it does not anticipate (which prints a traceback).
* EXIT_STOPPED: it stopped short, and invoking it again goes on once the
  reason is dealt with: a pass paused by the engine or its cache
  (yizkr.pipeline.llm.PassPaused), a run's control file, or, for
  `ingest run`, a document awaiting a review gate.
* EXIT_REFUSED: it refused before doing what it was asked, and printed
  why: a usage error, an input it will not act on, or a run directory
  that records other settings (yizkr.pipeline.rundir.RunSettingsConflict).
  Invoking it again unchanged is refused again.
* EXIT_INTERRUPTED: Ctrl-C.

The scratch commands give the refusals of their driver
(yizkr.pipeline.scratch.ScratchRefused, and RunSettingsConflict)
EXIT_FAILED, and `scratch estimate` a pause too, since it makes no call
that could go on.
"""

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_STOPPED = 2
EXIT_REFUSED = 3
EXIT_INTERRUPTED = 130
