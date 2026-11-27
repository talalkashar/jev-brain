#!/bin/sh
# Run jev-brain consent yourself first. Prepare a new, empty TextEdit document.
# The delay gives you time to bring that document forward and focus its text area.
set -eu
printf '%s\n' 'Switch to your empty TextEdit document. The step starts in five seconds.'
sleep 5
exec jev-brain use --expect TextEdit --target 'focused field' \
    --request 'Write a greeting in the empty document' --text 'Hello from jev-brain.' --act
