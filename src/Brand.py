"""The one colour the bot answers in.

Every embed that has no reason to be a particular colour is this one, so a reply from Newt is
recognisable before the title is read. It matches `--mint` in the dashboard's stylesheet and
the default the message builder hands out, and those three are meant to stay the same value.

Colours that mean something keep meaning it. Red for a ban, amber for a warning, grey for
somebody leaving, pink for a marriage: those are a legend, and painting them all mint would
throw the legend away. Cogs go on keeping their own named palette for that.

The rule is that a colour is either carrying information or it is MINT. There is no third
case, which is why nothing in here is called COLOR_INFO or COLOR_DEFAULT any more: a default
that has a name of its own drifts, and this one had drifted to blurple in nine cogs.
"""

MINT = 0x3DDC97
