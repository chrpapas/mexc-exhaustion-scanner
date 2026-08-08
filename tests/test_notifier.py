from app.notifier import DiscordNotifier


def test_default_discord_signal_levels_are_quiet():
    notifier = DiscordNotifier(None)
    assert not notifier.should_send_signal("run_watch")
    assert notifier.should_send_signal("exhaustion_watch")
    assert not notifier.should_send_signal("breakdown_watch")
    assert notifier.should_send_signal("confirmed_short")


def test_discord_signal_levels_are_configurable():
    notifier = DiscordNotifier(None, {"confirmed_short"})
    assert not notifier.should_send_signal("exhaustion_watch")
    assert notifier.should_send_signal("confirmed_short")
