"""Every piece of text the pages show, in one place, so that an Irish version
of the pages can be added without touching the templates (plan, D10).

Templates use {{ t.key }}. Keep the wording plain: the reader is not technical.
"""

EN = {
    "site_name": "Ask about Ireland",
    "skip": "Skip to the main part of the page",
    "nav_ask": "Ask a question",
    "nav_about": "How it works",

    # Front page
    "home_title": "Ask a question about Ireland",
    "home_lead": "Type your question in ordinary words. The answer comes from the official "
                 "figures of the Central Statistics Office, and you can see exactly where "
                 "each number came from.",
    "question_label": "Your question",
    "question_hint": "For example: How many people live in Leitrim?",
    "ask_button": "Get an answer",
    "examples_title": "Questions to try",
    "examples": [
        "How many people live in Leitrim?",
        "Which county has the highest share of people aged 65 and over?",
        "How has the number of people speaking Irish every day changed?",
        "What is the average rent in Galway city?",
        "How many cattle are there in Cork compared with Kerry?",
        "What share of homes in Dublin are heated by gas?",
    ],
    "own_key_summary": "Use your own Claude or OpenAI key",
    "own_key_help": "Answers here are free and come from a low-cost service. If you have an "
                    "API key of your own you can use it instead, and you pay that company "
                    "for your questions. The key is used for your question and then "
                    "forgotten. We never store it.",
    "own_key_provider": "Service",
    "own_key_model": "Model name (leave empty for the usual one)",
    "own_key_key": "Your API key",
    "provider_site": "The site's free service",
    "home_note": "Answers can take up to a minute. The figures are a copy of the CSO's "
                 "published tables taken in October 2026.",

    # Answer page
    "working_title": "Working on your answer",
    "working_wait": "This usually takes under a minute. You can leave this page open, "
                    "or come back to it later at the same address.",
    "working_first": "Reading your question",
    "you_asked": "You asked",
    "answer_heading": "Answer",
    "download_excel": "Download as Excel",
    "download_csv": "or as CSV",
    "table_rows_note": "Showing all {n} rows.",
    "sources_heading": "Where these figures come from",
    "source_line": "Central Statistics Office table {code}, covering {period}. "
                   "Last updated by the CSO on {updated}.",
    "source_open": "Open this table on the CSO website",
    "no_sources": "No figures were used in this answer.",
    "how_summary": "How this was worked out",
    "how_intro": "These are the steps taken to answer your question, in order. The queries "
                 "are shown so that anyone can check the figures.",
    "answered_by": "Answered by {model} in {seconds} seconds on {date}.",
    "answered_own_key": "Your own key was used for this answer.",
    "feedback_q": "Was this answer right?",
    "feedback_yes": "Yes, it was right",
    "feedback_no": "No, something was wrong",
    "feedback_thanks": "Thank you. Your reply helps us find and fix wrong answers.",
    "check_note": "Answers are written by a computer program from the official figures. "
                  "Check the source before relying on a number.",
    "ask_another": "Ask another question",
    "failed_title": "This question could not be answered",
    "failed_generic": "Something went wrong while working on the answer. Nothing is wrong "
                      "with your question. Please try it again.",
    "failed_setup": "The answering service is not available at the moment. Please try again later.",
    "failed_interrupted": "The site was restarted while this answer was being worked on. "
                          "Please ask the question again.",
    "try_again": "Ask it again",

    # Limits and errors
    "err_empty": "Type a question first.",
    "err_long": "That question is too long. Keep it under {n} characters.",
    "err_daily": "You have asked {n} questions today, which is the daily limit while the "
                 "site is being tested. Come back tomorrow, or use your own key.",
    "err_month": "This month's free questions have all been used. The site pays for each "
                 "answer and has reached its budget. Free questions start again on the "
                 "first of next month. You can still ask with your own key.",
    "err_busy": "A lot of questions are being answered right now. Yours will start in a moment.",
    "err_key": "To use your own key, choose the service and paste the key.",
    "not_found_title": "There is nothing at this address",
    "not_found": "The answer you are looking for may have been removed, or the address may "
                 "be mistyped.",

    # Step descriptions shown while working, and in "how this was worked out"
    "step_search": "Looking for tables about: {query}",
    "step_describe": "Reading the table “{title}”",
    "step_describe_find": "Looking in “{title}” for “{find}”",
    "step_place": "Checking the place name “{name}”",
    "step_sql": "Getting the figures",
    "step_sql_error": "A query did not work; trying another way",
    "step_table": "Preparing the table “{title}”",
    "step_writing": "Writing the answer",

    # About page
    "about_title": "How it works",
    "footer": "Figures: Central Statistics Office, published under the CSO's open data "
              "licence. This site is not run by the CSO or any public body.",
}


class _Text:
    def __init__(self, table):
        self._t = table

    def __getattr__(self, key):
        try:
            return self._t[key]
        except KeyError:
            raise AttributeError(key) from None


t = _Text(EN)
