# Consumers of FilesWatcher

The class `FilesWatcher` is consumed by exactly two files in the
Node.js source tree.

consumer: lib/internal/main/watch_mode.js:22
consumer: lib/internal/test_runner/runner.js:45

Both consumers import the class and instantiate it (`new FilesWatcher(...)`)
to drive their watch loops. No other source file constructs or
imports the class.
