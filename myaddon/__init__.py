# import the main window object (mw) from aqt
from aqt import mw
# import the "show info" tool from utils.py
from aqt.utils import showInfo
# import all of the Qt GUI library
from aqt.qt import QAction, qconnect

# We're going to add a menu item below. First we want to create a function to
# be called when the menu item is activated.

def testFunction() -> None:
    # mw is annotated as `AnkiQt` but assigned None until Anki's GUI finishes
    # starting, so type checkers see it as possibly-None. Anki only calls this
    # from a menu action, by which point a collection is always loaded, so
    # assert to narrow the type and satisfy both mypy and Pylance.
    col = mw.col
    assert col is not None
    # get the number of cards in the current collection, which is stored in
    # the main window
    cardCount = col.card_count()
    # show a message box
    showInfo("Card count: %d" % cardCount)

# create a new menu item, "test"
action = QAction("test", mw)
# set it to call testFunction when it's clicked
qconnect(action.triggered, testFunction)
# and add it to the tools menu
mw.form.menuTools.addAction(action)
