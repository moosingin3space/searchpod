"""Example prompts the root-page setup wizard offers.

Each one walks an end-to-end flow through the MCP tools and ends on a
turn that renders one of the app's UIs, so a new user sees the visual
half of the app and not just tool output.
"""

from reboot.application import ExamplePrompt

example_prompts = [
    ExamplePrompt(
        title="Add your first show and episode",
        prompts=[
            "Add the podcast 'Signals & Noise' to my catalog — its feed "
            "is https://example.com/signals.xml and it's a weekly show "
            "about computing history.",
            "Add an episode to it: 'The Analytical Engine', published "
            "2024-03-05, about Babbage's designs and their reception. It "
            "has chapters 'Cold open' at 0 seconds, 'Babbage's designs' "
            "at 240 seconds, and 'Punch cards' at 1180 seconds, and the "
            "guest is Ada Lovelace.",
            "Show me that episode.",
        ],
    ),
    ExamplePrompt(
        title="Find the episode that covered a topic",
        prompts=[
            "Which episode talked about punch cards?",
            "Show me that episode so I can see where in it that comes up.",
        ],
    ),
    ExamplePrompt(
        title="Find out who the guest was",
        prompts=[
            "When Signals & Noise covered Babbage's designs, who was the "
            "guest?",
            "Show me that episode.",
        ],
    ),
    ExamplePrompt(
        title="See what someone has talked about",
        prompts=[
            "Look up Ada Lovelace in my catalog.",
            "Show me everything she's talked about.",
        ],
    ),
]
