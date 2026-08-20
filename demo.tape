# Regenerates the README demo. Run:
#   vhs demo.tape
#   gif2webp -mixed -q 90 -m 6 -mt /tmp/dcimport-demo.gif -o .assets/demo.webp
#
# Requires:
#   - vhs and gif2webp installed
#   - an iPhone connected over USB, unlocked, and trusted
#   - `dcimport` on PATH (e.g. `uv tool install .` from this repo)
#
# The README displays this 1800 px recording at exactly 900 CSS pixels. The 2:1
# ratio keeps terminal text sharp on Retina displays and avoids fractional scaling.

Output "/tmp/dcimport-demo.gif"

Set Shell "bash"
Set FontSize 34
Set Width 1800
Set Height 360
Set Padding 32
Set Framerate 20
Set CursorBlink false
Set TypingSpeed 50ms
Set Theme "Catppuccin Mocha"

# --- off-camera setup: clean prompt + fresh output dir (so it's a real import) ---
Hide
Type `export PS1="\$ "`
Enter
Type "rm -rf /tmp/dcimport-demo"
Enter
Ctrl+L
Show

# --- the demo ---
Type "dcimport /tmp/dcimport-demo"
Sleep 200ms
Enter
Sleep 30s

# --- optional extra beats: uncomment to also show a custom layout ---
# Type "dcimport /tmp/dcimport-demo-by-date --layout '{mtime:%Y}/{mtime:%m}/{name}'"
# Sleep 700ms
# Enter
# Sleep 12s
# Sleep 2s
