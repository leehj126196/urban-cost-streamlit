# Python starts this module automatically when the repository root is on sys.path.
# It installs the VWorld retry/fallback patch before the Streamlit app runs.
try:
    import vworld_patch  # noqa: F401
except Exception:
    pass

# UI override: let the user choose only the two CRS values used in this workflow.
try:
    import streamlit as st

    _original_text_input = st.text_input

    def _patched_text_input(label, *args, **kwargs):
        if label == "DXF/SHP 원본 EPSG 번호":
            help_text = kwargs.get("help", "DXF 원본 좌표계를 선택하세요.")
            return st.selectbox(
                "DXF/SHP 원본 좌표계",
                options=["5174", "5186"],
                index=0,
                format_func=lambda x: f"EPSG:{x}",
                help=help_text,
                key="epsg_selector",
            )
        return _original_text_input(label, *args, **kwargs)

    st.text_input = _patched_text_input
except Exception:
    pass
