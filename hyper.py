"""Full Throttle Trading: the aggressive scalper (exhibition, not racing).

Same machinery, rules and $1,000 start as Rapid Fire (see scalper.py), set to attack:
always aggressive (every setup at full size, never stands down), more and smaller cash slices,
up to two new trades per 2-minute check, a wider and faster watch list (3x ETFs allowed, held
minutes and always flat by the close), quicker momentum triggers and shorter holds. It still
can't use margin or spend unsettled cash. If it reaches the goal it doesn't end the race."""
import scalper


def entries(ctx):
    return []


def manage(ctx, pos):
    return scalper.manage(ctx, pos, "hyper")
