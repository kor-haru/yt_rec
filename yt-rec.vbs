' Start yt-rec from a source checkout without a console window.
'
' Why this file exists: uv writes .venv\Scripts\pythonw.exe as a console
' subsystem trampoline (measured on uv 0.11.8), so launching the GUI entry
' point through it still attaches an empty terminal. WScript.Shell.Run with
' window style 0 creates no window at all. See README and issue #99. Once uv
' ships a GUI trampoline this file can be deleted; nothing in the app imports
' it.
'
' ASCII only, on purpose: Windows Script Host reads .vbs in the system ANSI
' code page, so non-ASCII text in this file would reach the user as mojibake.

Option Explicit

Dim shell, fso, target
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Relative to this file, so the checkout can live anywhere.
target = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), ".venv\Scripts\yt-rec.exe")

If Not fso.FileExists(target) Then
    ' Quitting silently would be indistinguishable from the app failing to start.
    MsgBox "yt-rec was not found at:" & vbCrLf & target & vbCrLf & vbCrLf & _
           "Run 'uv sync' in the project folder first.", vbExclamation, "yt-rec"
    WScript.Quit 1
End If

' 0 hides the window, False returns without waiting for the app to exit.
shell.Run """" & target & """", 0, False
