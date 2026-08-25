"""Small, reusable GTK widgets for lan-copier.

- `dirpane.DirPane`: the browse tree (columns, filtering, checkbox selection,
  comparison-state coloring) shared by the SOURCE and DESTINATION sides.
- `endpoint.EndpointBar`: the merged single-row bar per side — connection
  selector + edit/connect controls + (once connected) the path field and the
  file-action buttons, plus a colour-coded connection status dot.
- `dialog.ConnectionDialog`: the modal SSH endpoint editor.
"""