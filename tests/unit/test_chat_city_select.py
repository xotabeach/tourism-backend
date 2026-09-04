"""The start city is asked with a dropdown, and asked even when the model forgets.

Regression cover for a live bug: Gemini asked "в каком городе или районе Крыма"
in prose while its structured ask_field said something else, so the backend
built generic chips and the user was left with a question and no way to answer.
"""

from tourism_backend.modules.route_builder.application.chat_actions import (
    ask_field_from_text,
    build_actions_block,
    city_options,
    interactive_control_blocks,
)
from tourism_backend.modules.route_builder.application.schemas import SelectBlockOut


class TestCitySelectBlock:
    def test_city_ask_field_yields_a_select_with_every_city(self) -> None:
        blocks = interactive_control_blocks(ask_field="city")

        selects = [block for block in blocks if isinstance(block, SelectBlockOut)]
        assert len(selects) == 1
        select = selects[0]
        assert select.id == "city"
        assert select.placeholder == "Город"
        assert [option.label for option in select.options] == [label for _, label in city_options()]
        assert "Симферополь" in {option.value for option in select.options}

    def test_options_come_from_the_action_catalog(self) -> None:
        # One source of truth: chips and dropdown cannot drift apart.
        assert ("Ялта", "Ялта") in city_options()
        assert len(city_options()) == 10

    def test_an_already_chosen_city_preselects_the_dropdown(self) -> None:
        blocks = interactive_control_blocks(ask_field="city", constraints={"city": "Судак"})

        select = next(block for block in blocks if isinstance(block, SelectBlockOut))
        assert select.value == "Судак"

    def test_a_region_is_not_mistaken_for_a_chosen_city(self) -> None:
        # The model puts "Крым" in constraints — a region, not a city. Shown
        # as the dropdown's value it looked like a choice already made.
        blocks = interactive_control_blocks(ask_field="city", constraints={"city": "Крым"})

        select = next(block for block in blocks if isinstance(block, SelectBlockOut))
        assert select.value is None
        assert select.placeholder == "Город"

    def test_other_ask_fields_get_no_select(self) -> None:
        for field in ("pace", "budget", "interests", "ready"):
            blocks = interactive_control_blocks(ask_field=field)
            assert not [b for b in blocks if isinstance(b, SelectBlockOut)], field

    def test_city_chips_are_gone_now_that_the_dropdown_exists(self) -> None:
        # Two mechanisms for one question is what produced the confusing UI.
        blocks = build_actions_block(ask_field="city")

        labels = {action["label"] for block in blocks for action in block.actions}
        assert "Симферополь" not in labels
        assert "Ялта" not in labels


class TestAskFieldFallback:
    def test_prose_about_the_city_wins_when_the_model_set_nothing(self) -> None:
        text = (
            "Отлично, люблю активный темп! Чтобы я подобрал идеальный маршрут, "
            "подскажите, в каком городе или районе Крыма вы планируете отдыхать?"
        )
        assert ask_field_from_text(text, None) == "city"
        assert ask_field_from_text(text, "ready") == "city"

    def test_a_deliberate_field_is_never_second_guessed(self) -> None:
        text = "В каком городе вы сейчас?"
        # The model asked about pace on purpose — prose must not override it.
        assert ask_field_from_text(text, "pace") == "pace"

    def test_unrelated_prose_changes_nothing(self) -> None:
        assert ask_field_from_text("Какой темп вам ближе?", None) is None
        assert ask_field_from_text(None, "budget") == "budget"

    def test_already_city_stays_city(self) -> None:
        assert ask_field_from_text("что угодно", "city") == "city"
