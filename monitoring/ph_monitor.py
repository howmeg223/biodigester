"""Pure pH presentation helpers. These never publish actuator commands."""
import math


def validate_limits(low, high):
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) for v in (low, high)):
        return "Enter numeric values for both pH limits."
    if not 0 <= low < high <= 14:
        return "Use limits between 0 and 14, with minimum lower than maximum."
    return None


def classify(value, low, high):
    if value < low:
        return "Low pH", "#b45309"
    if value > high:
        return "High pH", "#b3261e"
    return "Within limits", "#2e7d32"
