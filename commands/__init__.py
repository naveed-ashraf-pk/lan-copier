"""Remote/local command builders for lan-copier.

The TransferEngine and the endpoint classes never interpolate user paths into
shell commands directly. Every shell-facing operation is expressed through the
pure builder functions in this package, which are unit-tested for exact output
and take care of quoting (shlex.quote for POSIX, base64 -EncodedCommand for
PowerShell, plain os/shutil calls for the local endpoint).

Each submodule is independent of GTK and of the transport classes.
"""