from naiwa.geometry import integer_scale, visible_position


def test_scale_stays_on_whole_pixels():
    assert integer_scale(1) == 1
    assert integer_scale(1.5) == 2
    assert integer_scale(2.5) == 3
    assert integer_scale(9) == 4


def test_offscreen_pet_returns_to_a_visible_screen():
    screens = [(0, 0, 800, 600), (1920, 0, 800, 600)]
    assert visible_position(100, 100, 40, 40, screens) == (100, 100)
    x, y = visible_position(-4000, 9000, 40, 50, screens)
    assert 0 <= x <= 800 - 40
    assert 0 <= y <= 600 - 50


def test_partial_visibility_is_clamped_on_the_correct_monitor():
    screens = [(-1200, 0, 1200, 900), (0, 0, 1920, 1080)]
    assert visible_position(-1199, 880, 200, 160, screens) == (-1199, 740)
    assert visible_position(1919, 900, 200, 160, screens) == (1720, 900)


def test_scale_rounds_in_physical_pixels_instead_of_scaling_dpi_twice():
    assert integer_scale(1.25, 3) == 4
    assert integer_scale(1.5, 3) == 5
    assert integer_scale(2, 3) == 6
