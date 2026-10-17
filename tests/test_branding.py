"""Branding and premium command-center surface.

Two things are checked here, both against the real code paths:

* :mod:`arabic_agent.branding` is the single source of truth for the *public*
  identity (Super AI Agent / SUPER AI), and the on-disk identity is deliberately
  left alone so an upgrade keeps a user's data.
* The desktop page is the premium command center the design calls for: an
  assistant presence, a real voice surface, an Arabic-first shell, and no
  invented data — every number it renders comes from the bridge.
"""

from __future__ import annotations

import re

from arabic_agent import branding
from arabic_agent.desktop.shell import _load_index_html


def test_public_brand_is_super_ai_agent() -> None:
    assert branding.PRODUCT == "Super AI Agent"
    assert branding.BRAND == "SUPER AI"
    assert branding.PRODUCT_ID == branding.PRODUCT
    assert branding.WINDOW_TITLE == "Super AI Agent — SUPER AI"


def test_arabic_identity_is_first_class() -> None:
    # Arabic is authored first, not translated after.
    assert branding.PRODUCT_AR
    assert branding.BRAND_AR
    assert branding.product_name("ar") == branding.PRODUCT_AR
    assert branding.product_name("ar-SA") == branding.PRODUCT_AR
    assert branding.product_name("en") == branding.PRODUCT
    assert branding.brand_name("ar") == branding.BRAND_AR
    assert branding.brand_name("en") == branding.BRAND


def test_ondisk_identity_is_not_renamed() -> None:
    """The rename is presentation only; storage identity must not move.

    Changing the data directory, the uninstall key or the distribution name
    would strand a user's runs, memory and documents on upgrade, so those stay.
    """
    from arabic_agent.desktop.installer import UNINSTALL_KEY
    from arabic_agent.desktop.shell import SINGLE_INSTANCE_MUTEX

    assert "ArabicAIAgent" in SINGLE_INSTANCE_MUTEX
    assert "ArabicAIAgent" in UNINSTALL_KEY
    assert branding.PRODUCT not in SINGLE_INSTANCE_MUTEX


def test_ui_has_an_assistant_presence() -> None:
    """The character is a real surface, driven by the shipped viseme map."""
    html = _load_index_html()
    assert 'id="character"' in html
    assert 'id="characterMouth"' in html
    assert "animateMouth" in html
    # The mouth is animated from the character's own viseme data, not a random
    # flap and not a second animation library.
    assert "character.visemes" in html
    assert "lookup.set" in html
    for state in ("listening", "thinking", "speaking", "waiting_approval", "error", "success"):
        assert f'"{state}"' in html


def test_ui_voice_surface_is_wired_to_the_bridge() -> None:
    html = _load_index_html()
    for hook in (
        'id="voiceListenBtn"',
        'id="voiceStopBtn"',
        'id="voiceSpeakBtn"',
        'id="voiceRunBtn"',
        'id="voiceHeard"',
        'id="voiceReply"',
        'id="voiceModels"',
        'id="voiceActions"',
        'id="voiceDevice"',
    ):
        assert hook in html
    # Every voice control calls the bridge; none is a placeholder.
    assert "a.voice_transcribe(" in html
    assert "a.voice_stop(" in html
    assert "a.voice_speak(" in html
    assert "a.voice_start(" in html
    assert "a.voice_models()" in html
    assert "a.voice_devices()" in html
    assert "a.install_voice_model(" in html
    assert "a.remove_voice_model(" in html


def test_ui_speaks_only_after_a_real_run_outcome() -> None:
    """The reply reflects the run's stored result, never a predicted string."""
    html = _load_index_html()
    assert "a.get_run(started.run_id)" in html
    assert "run.result || run.error" in html


def test_ui_is_bilingual_and_rtl_aware() -> None:
    html = _load_index_html()
    # Both locales ship in the page and direction follows the active language.
    assert 'LANG === "ar" ? "rtl" : "ltr"' in html
    assert "setLang" in html
    assert "aap_lang" in html
    assert "aap_theme" in html
    # A light theme exists as a token override, not a second stylesheet.
    assert '[data-theme="light"]' in html


def test_ui_navigation_is_a_small_destination_set() -> None:
    """A premium command center has few destinations, not a tab dump."""
    html = _load_index_html()
    sections = re.findall(r'id="s-([a-z-]+)"', html)
    # The four primary destinations plus voice and settings.
    for expected in ("home", "agents", "projects", "activity", "voice", "settings"):
        assert expected in sections, expected
    # Operational surfaces are grouped, not top-level destinations.
    assert 'data-pane="advanced"' in html
    assert 'data-pane="models"' in html


def test_ui_renders_honest_readiness() -> None:
    html = _load_index_html()
    # Available / degraded / unavailable are the service's real states.
    assert 't("available")' in html
    assert 't("degraded")' in html
    assert 't("unavailable")' in html
    assert 't("missingTools")' in html


def test_ui_carries_no_network_origin() -> None:
    html = _load_index_html()
    assert "default-src 'none'" in html
    assert "connect-src 'none'" in html
    assert "frame-src 'none'" in html
    assert "object-src 'none'" in html


def test_product_string_reaches_the_page() -> None:
    """The About panel names the product from the branding source."""
    html = _load_index_html()
    assert branding.PRODUCT in html
    assert branding.BRAND in html
