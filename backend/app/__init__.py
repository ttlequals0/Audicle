import warnings

from app.version import __version__

# trafilatura imports urllib3's SOCKS support, which warns when PySocks is absent. Audicle never uses it.
warnings.filterwarnings("ignore", message="SOCKS support in urllib3", module=r"urllib3\.contrib\.socks")

__all__ = ["__version__"]
