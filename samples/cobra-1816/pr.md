# fix: correct isFlagArg logic to avoid false flag detection

## Summary
Fixes completion incorrectly treating arguments that contain a dash as the second character (e.g., `1-ff00:0:1`) as flags, causing errors during auto-completion.

## Root cause
`isFlagArg` incorrectly identified any argument with a dash at index 1 as a flag:
```go
return ((len(arg) >= 3 && arg[1] == '-') ||
        (len(arg) >= 2 && arg[0] == '-' && arg[1] != '-'))
```
This logic missed the requirement that a flag must start with a leading dash (`-`). Consequently, non-flag arguments like `1-ff00:0:1` were mis-classified.

## Fix
Update `isFlagArg` to first verify that the argument starts with a dash before checking for a long-form flag (`--`). The new implementation:
```go
return ((len(arg) >= 3 && arg[0] == '-' && arg[1] == '-') ||
        (len(arg) >= 2 && arg[0] == '-' && arg[1] != '-'))
```
Now only strings beginning with `-` are considered flags, eliminating the false positives.

## Testing
Added a reproducer test (see issue discussion) that runs the root command with `__complete` and an argument like `1-ff00:0:1`. The command now completes without emitting the spurious flag error and returns the expected `ShellCompDirectiveDefault`. Existing completion tests continue to pass.

Refs spf13/cobra#1816
