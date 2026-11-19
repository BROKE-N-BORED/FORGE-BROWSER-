"""User help topics for the How to Use window (R5-03).

Plain help written for the person using the browser, shown by
the Help menu's How to Use window. This is NOT the developer
manual: BROWSER.md stays the manual (howto.py indexes it, and
the local /howto page shows it), and the window still groups
the manual's developer topics after these. What changed in
R5-03 is what a first-time user meets first: topics about
using the browser, in plain words, instead of the manual's
own sections.

Standard library only, like everything in the engine room.
"""

# (title, body) pairs, in the order the window lists them.
# Bodies are plain text; paragraphs are separated by blank
# lines. No em-dashes (U33) and no developer terms: if a topic
# needs a word the user has not met, the topic explains it.
_TOPICS = (
    ("Welcome to Forge Browser",
     "Forge Browser is your own web browser. It runs on this "
     "PC, with a local AI helper built in. There are no "
     "accounts and no API keys, and nothing you do here leaves "
     "this machine.\n"
     "\n"
     "This window is the short guide. Pick a topic on the left "
     "to read it."),
    ("Tabs and the address bar",
     "To open a page, type an address in the address bar at "
     "the top and press Enter. If you type plain words instead "
     "of an address, the browser searches for them.\n"
     "\n"
     "Tabs hold several pages at once. The + Tab button opens "
     "a new one, the x on a tab closes it, and Ctrl+Tab moves "
     "between them. The arrow buttons go back and forward, "
     "the round arrow reloads the page, and the house button "
     "goes to the start page.\n"
     "\n"
     "Press Ctrl+L to jump to the address bar, and Ctrl+F to "
     "find a word on the current page."),
    ("Searching and reading pages",
     "Word searches open a results page put together on this "
     "machine. Click a result to open it.\n"
     "\n"
     "The browser can also read a page as clean text, without "
     "the ads and clutter. That is what the chat helper uses "
     "when you ask about a page, and it is why its answers can "
     "quote the page you are looking at."),
    ("The chat helper",
     "The panel on the right is the chat helper. It runs on a "
     "model stored on this PC, so it works offline and costs "
     "nothing.\n"
     "\n"
     "Ask it in plain words. It can search the web, open "
     "pages, read the page you are on, summarize a page, run "
     "longer research in the background, and remember facts "
     "you tell it. The Summarize button sums up the current "
     "page without you typing anything.\n"
     "\n"
     "Before its first step in a turn, the helper shows a "
     "plan and waits for your yes. Some steps, like research "
     "or remembering something, ask every time. If you say "
     "no, the step is skipped and the log says so.\n"
     "\n"
     "Stop ends a turn early. The Models and AI section of "
     "the panel picks which model does each job, and the AI "
     "switch turns every AI feature off at once."),
    ("Downloads",
     "When a page offers a file, the browser saves it to your "
     "Downloads folder. The Downloads button at the top shows "
     "what is coming down and what has finished.\n"
     "\n"
     "In the Downloads window you can open a finished file, "
     "show it in its folder, or retry one that failed. To "
     "save the current page as a PDF, use Save as PDF in the "
     "File menu and pick where it goes."),
    ("Bookmarks and history",
     "The star button at the top saves the current page as a "
     "bookmark; Ctrl+D does the same. Bookmarks live in the "
     "Bookmarks menu, where you can open or remove them.\n"
     "\n"
     "The browser keeps a list of pages you visit. Open it "
     "from the History menu to search old visits, and clear "
     "it there too. Pages on this machine itself are not "
     "recorded."),
    ("The vault",
     "The vault keeps your site logins in one encrypted file, "
     "locked with a passphrase you choose. Open it from the "
     "Tools menu.\n"
     "\n"
     "Unlock the vault and the browser can fill a saved login "
     "when a page asks for one. Passwords are never shown on "
     "screen. Lock the vault when you are done, and the "
     "logins stay sealed until the passphrase is entered "
     "again."),
    ("The doorway",
     "The doorway lets another program on this PC hand Forge "
     "Browser a browsing job and get the result back. Fyrn, "
     "the local model runner, uses it this way: Fyrn asks for "
     "a page or a search, Forge Browser does the browsing, "
     "and Fyrn gets the page text and a screenshot back. It "
     "only listens on this machine, so nothing about it is "
     "reachable from the internet."),
    ("Updates",
     "Forge Browser checks quietly for a newer version when "
     "it starts. If one is out, it asks first; nothing "
     "downloads until you say yes. The download is checked "
     "against its published checksum before it installs, and "
     "after installing it asks whether to restart now or wait "
     "until the next launch.\n"
     "\n"
     "You can check right away from the Help menu, and the "
     "same menu can turn the start-up check off."),
    ("Where your files live",
     "Your things stay on this PC, in your home folder. "
     "Settings, bookmarks, the vault, remembered facts, "
     "history, the chat log, and research reports live in "
     "the .forge folder there. Downloads go to your Downloads "
     "folder.\n"
     "\n"
     "Nothing is uploaded anywhere. The History menu can "
     "clear the visit list, the Bookmarks viewer can remove "
     "saved pages, and the chat panel has a Clear chat "
     "button for the log."),
)


def user_help_topics():
    """[(title, body)] — the How to Use window's user topics,
    in list order. Always available: the help ships with the
    program, not with the manual file."""
    return list(_TOPICS)
