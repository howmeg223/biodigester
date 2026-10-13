"""Small data containers shared by the controller's fault checks."""
from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    """Named levels of seriousness. An Enum limits the available choices."""
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class FaultCategory(str, Enum):
    """Which part of the system a fault belongs to."""
    SENSOR = "SENSOR"
    ACTUATOR = "ACTUATOR"
    PROCESS = "PROCESS"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True)
class FaultDefinition:
    """Fixed rules for a fault. frozen=True prevents changing these rules.

    A dataclass creates the constructor from the fields listed below.
    Inhibiting heating means forcing the normal heater command off. A latching
    fault stays active until its condition clears and the operator acknowledges it.
    """
    code: str
    category: FaultCategory
    severity: Severity
    message: str
    action: str
    inhibit_heating: bool = False
    trip_safety: bool = False
    latching: bool = False


@dataclass
class FaultState:
    """The changing status of one fault, alongside its fixed definition."""
    definition: FaultDefinition
    active: bool = False
    condition_active: bool = False
    acknowledged: bool = False
    first_detected: float | None = None
    last_changed: float | None = None

    def as_dict(self):
        """Convert the fault to a dictionary that can be sent as JSON."""
        definition = self.definition
        return {
            "code": definition.code,
            "category": definition.category.value,
            "severity": definition.severity.value,
            "message": definition.message,
            "action": definition.action,
            "active": self.active,
            "condition_active": self.condition_active,
            "acknowledged": self.acknowledged,
            "first_detected": self.first_detected,
            "last_changed": self.last_changed,
            "inhibit_heating": definition.inhibit_heating,
            "trip_safety": definition.trip_safety,
            "latching": definition.latching,
        }
