# Python starts this module automatically when the repository root is on sys.path.
# It installs the VWorld retry/fallback patch before the Streamlit app runs.
try:
    import vworld_patch  # noqa: F401
except Exception:
    pass
