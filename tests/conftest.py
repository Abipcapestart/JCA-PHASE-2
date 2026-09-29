import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_DIR = os.path.dirname(_THIS_DIR)
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)

import pytest  # noqa: E402


@pytest.fixture
def eu27_minus(request):
    """Return the 27 EU states minus whichever ones are passed as param."""
    from p2_config import EU_27_MEMBER_STATES
    excluded = getattr(request, "param", [])
    return [s for s in EU_27_MEMBER_STATES if s not in excluded]
