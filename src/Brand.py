"""The one colour the bot answers in.

Every embed is this colour. Not most of them, not the ones without a better idea: all of them.
It matches `--mint` in the dashboard's stylesheet and the default the message builder hands
out, and those three are meant to stay the same value.

There was a legend once, red for a ban and amber for a warning and grey for somebody leaving,
on the reasoning that a busy log channel is scannable by colour. It went, because in practice
it was fifteen shades across nineteen files and the bot looked like it had been assembled by
several people who had never met. A consistent bot that needs its titles read beats a
patchwork that can be skimmed.

So there is nothing to decide when writing a new embed, and nothing to keep in step. If you
find yourself reaching for a second colour, the thing you want is probably an emoji at the
front of the line or a clearer title.
"""

MINT = 0x3DDC97
