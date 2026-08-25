# Changelog

Notable changes to this fork. Versions are the fork's own release line (root
`VERSION` file, `vX.Y.Z` tags), independent of upstream StreamController's
`app_version` in `globals.py`. Each release publishes an installable flatpak
bundle as a release asset.

## [Unreleased]

### Added

- Drag an action in the sidebar to reorder it. Hold an action row, drag it over
  another row, and a line marks the edge it lands on. The release moves it
  there and the page keeps the new order. The deck you are editing loads the
  page again, so its key runs the actions in the new order. Another deck
  showing the same page keeps the old order until it loads that page again.
  Each action takes its own settings, its comment and its event assignments
  with it, and the media, background and label permissions stay with the action
  that holds them, including on a key whose actions did not all load. The up
  and down buttons on each row make the same move, so a reorder needs no
  pointer.
  there, the key runs its actions in the new order straight away, and the page
  keeps the order. Each action takes its own settings, its comment and its
  event assignments with it, and the media, background and label permissions
  stay with the action that holds them. The up and down buttons on each row
  make the same move, so a reorder needs no pointer.
- Keys can hold their size while they are pressed. The general settings carry
  a "Shrink keys while pressed" switch, and turning it off leaves a held key
  drawing exactly the picture it draws at rest, which suits a page whose keys
  carry one image between them and whose seams the shrink breaks. It is on by
  default, so a deck nobody configures presses as it always has, and a change
  reaches the next press with no restart.

- Press a key from outside Deckard. `--emulate-input SERIAL PAGE COORDS press`
  runs that key's actions through the deck's own input path, so they receive
  the events a press produces, and `long-press` holds the key past the hold
  time so the hold actions run too. The key is named by the same coordinates
  `--change-state` uses. The page is switched to first when it is not the one
  showing, and the press is dropped with a reason rather than made if the deck
  leaves that page before it lands, if the key is already held, if the session
  locks in the meantime, or if the press cannot reach the deck in time. It needs Deckard to be running already, and says so
  instead of starting it: a press happens at a moment, so it cannot be held
  for a deck that is not plugged in yet. The same press is on the session bus
  as `EmulateInput`, beside `ChangePage` and `ChangeState`.

- A fake deck can take the shape of a real model. `--fake-deck-model` gives
  each fake deck the key grid, dials, touchscreen and screen of a Stream Deck
  Original, MK.2, Mini, XL, Plus, Neo or Pedal, and the deck says which model
  it stands for, so a page can be laid out for hardware that is not on the
  desk. Repeat the flag once per fake deck; the last name given covers the
  rest, and the name `default` leaves a deck with the shape and the key layout
  it already has. Without the flag every fake deck keeps the shape it has
  always had.
- The FPS row in the background editor now covers animated GIFs on a key,
  and carries a revert arrow. The rate is a limit on how often the key is
  redrawn, so lowering it drops frames: the animation still takes the same
  time end to end, but fewer of its frames are shown, and which ones survive
  depends on the rate you pick. Set it to save work on a busy page, not to
  slow a GIF down. The arrow appears once you set a rate below the maximum
  and clears it again, and the row then shows the rate the media itself runs
  at. Choosing the maximum clears the rate too, because it limits nothing. A
  page with no rate set is written exactly as before.
- A key or dial with several states now opens on the state it was last
  left on. The page keeps that state, so a page switch and the next start
  of the app show the state you left instead of the first one. A page
  whose inputs never leave their first state is written exactly as
  before, and still opens in an older version.
- The icon chooser searches every pack at once. Typing in the pack grid now
  shows the matching icons of all installed packs in one ranked list, each
  card naming the pack it came from, instead of asking you to open a pack
  first and search it alone. A query there searches icons across packs
  instead of narrowing the pack list, so typing leaves the pack grid;
  emptying the query brings it back. A search that matches nothing says so.
  The wallpaper and SD+ bar wallpaper choosers search the same way.

### Changed

- The revert arrow beside the frame-rate spinner in the background editor now
  arrives a moment after the rate settles, instead of at the first step of the
  edit. Spinning the rate down and back up no longer flashes the arrow on and
  off, and the spinner no longer shifts sideways under the pointer partway
  through a spin.
- The GtkHelper module no longer ships its silent disconnect helper. It
  swallowed every error around a signal disconnect, which hid real defects;
  the app's own rows now track their handlers instead. A plugin that imported
  the helper must disconnect by tracked handler id.
- A key whose picture fills its whole tile with nothing see-through now costs
  almost nothing while an animated wallpaper plays behind it. Such a key hides
  every pixel the wallpaper changes, so the app keeps the picture it drew and
  stops drawing it again on every frame. A grid of full-size icons over a video
  or GIF wallpaper is where this shows most. Anything the wallpaper can still
  show through, which is any picture with a transparent edge, a corner rounded
  by the size setting or a picture smaller than the tile, is drawn as before,
  and so is a key that is held down, carries a rolling label or plays its own
  video.
- `--change-page` and `--change-state` answer in a fraction of the time when
  Deckard is already running. Such a command needs one message to the running
  app, and it now sends it and leaves instead of first building a second copy
  of the whole app to throw away. A command that finds nothing running still
  starts the app and applies its request to the deck as it appears.
- A page or state command that cannot be carried out now says why and reports
  failure, where some of these ended in a crash message or in silence with a
  success code. That covers a state number too large to send, a page name or
  serial the terminal passed as bytes that are not text, and a session bus
  that cannot be opened. As before, one bad argument applies none of the
  command, and the message names the flag and the argument it came from.
- The page selector in the header opens a searchable list. Type any part of
  a page name to narrow it, walk the matches with the arrow keys and press
  Enter to open the best one. The list opens on the page the deck holds, and
  says so when nothing matches. A deck with many pages needed a scroll
  through the whole list before.
- The plugin store follows a reviewed snapshot of the official catalog,
  pinned by this fork and moved deliberately after a diff review. The
  catalog had been frozen since 2026-08 on an old snapshot because it
  tracked upstream's per-app-version mapping; the four new plugins and the
  twenty-one plugin updates published since then are available again, and
  the store no longer depends on that mapping file at run time.
- Installing or updating a store asset now refuses a download whose manifest
  requires a newer app version. Such an install would replace a working
  plugin with one the app then refuses to load; the running plugin now stays
  in place instead.
- The check that a deck is still plugged in now looks only at Stream Deck
  hardware. It used to scan every USB input device on the machine and open
  each one to read its name, several times a minute, which cost processor
  time and woke keyboards, mice, headsets and game controllers for nothing.
  The check now takes about a tenth of the time and touches no device but
  the deck.

### Fixed

- A key or the touch strip no longer keeps showing an out-of-date picture when
  two things repaint it at once. Whichever repaint drew first reached the deck
  last if it happened to be the slower one, and nothing corrected it, because
  the content it drew from had already been replaced. The most visible form was
  a key that stayed shrunken after the finger left it. Repaints of one key, or
  of the strip, now reach the deck in the order they were drawn.
- A plugin whose backend runs in its own environment says so when a system
  Python upgrade breaks it. Such an environment carries its own copy of Python,
  and an upgrade can leave that copy unable to start, so the backend never ran
  again and the plugin's actions stayed inert with nothing on screen to say
  why. The app now checks at launch whether that copy still starts. Rebuilding
  it means running the plugin's install scripts, which a starting app cannot
  ask you about, so it rebuilds only if you already set install scripts to run
  always. Otherwise it tells you which plugin needs a reinstall and changes
  nothing. An environment that still starts is left alone, whichever Python
  version built it, and a rebuild that fails puts the previous one back.

- A plugin no longer loses the first thing its backend reports. A backend takes
  a moment to connect, and an event the plugin raised in that moment reached
  nothing, so a device or state the backend reported at startup never showed on
  the keys until something made the plugin report it again. Such an event is
  now held briefly and delivered once the backend is up. The wait is short and
  fixed, a newer value always wins over a held one, and a backend that
  reconnects starts fresh instead of replaying what the failed attempt raised.
  Only the newest value of each event is kept, so a plugin that reports several
  different items under one event name in that moment delivers the last of
  them.

- The action list in the sidebar no longer fails to build for an action the app
  has just torn down, which left the list short or empty until the page was
  opened again.

- Installing a plugin from the store no longer turns other plugins' actions on
  your pages into a "no action holder found" placeholder. The install rebuilt
  the list of available actions in place, and a page that loaded during that
  rebuild found the list empty; the placeholder it stored then stayed until
  that page was loaded again.

- A plugin installed from the store is usable right away when its install
  brought its own requirements. The freshly installed requirement was
  invisible to the running app, so the plugin failed to load until a restart
  while the store reported a successful install. The reload now sees what the
  install put in place, and a plugin that still cannot load says so in an
  error message instead of reporting success.

- Labels render apostrophes and ampersands as themselves in every language.
  A French deck-settings label showed a code where its apostrophe belongs, and
  a German onboarding line showed a code for its ampersand, because every
  translated string was escaped whether or not the label reads markup. Plain
  labels now get plain text and only markup labels get escaped text. The
  removal dialog in the asset manager also shows its title again instead of a
  raw label key.

- Changing a fake deck's rows or columns now fills the new grid with the
  page's content right away. The resized grid stayed blank until the next page
  switch, and the replaced tiles released their media only when the garbage
  collector got to them. The next and back arrows in the asset chooser also no
  longer crash when clicked before the first pack loads; they stay disabled
  until there is something to page through.

- A page or asset whose name carries a character that looks like a digit but
  is not one, such as a superscript two, no longer breaks sorting. Sorting
  pages or assets with such a name raised an error and mis-sorted the list;
  those names now sort as text.

- The deck settings option that extends the background onto the touch strip
  shows its sentence in the app language instead of the raw label key.

- The tray icon shows the app icon instead of a placeholder square. The app
  pointed the desktop at the icon dir that ships with it, every time and
  whatever the app was installed from, and a desktop takes such a path over
  the icon theme you picked. A flatpak copy pointed it inside the sandbox, at
  a dir the desktop cannot read at all. An installed copy now keeps the icon
  the desktop already holds, and only a copy run from a source tree points the
  desktop at the icons that ship with it. Those icons now carry a theme file
  as well, which a desktop whose icon loader follows the icon theme spec to
  the letter, as a Qt one does, needs before it reads them.
- Killing the app during a download no longer leaves a half-written file
  behind. An image or video fetched from a url, and a plugin archive from the
  store, were written straight to their final name, so a kill part way through
  left a truncated file that the app later took for a complete one. A download
  now fills a temporary file and takes its final name in one step, so a killed
  download leaves nothing under that name and a failed one publishes nothing.
  A server that ends a body early without ever saying how long it should be
  still cannot be told from one that sent everything.
- Quitting no longer waits several seconds after the store has been open. The
  worker threads that fetch the store catalog parked for the life of the app,
  and the quit path waited its full bound for them before forcing the exit.
  The workers are now released at quit, before the store cache writes its
  index, so a quit is prompt and the cache index stays complete.

- The event-assignment list and the deck settings rows (background, brightness,
  saturation, rotation, screensaver and the state switcher) keep working after
  an error, the same hardening the sidebar rows received. A failure part way
  through updating one of them left the control silently dead until a restart.

- An animated key or dial that hits an error while starting one update no
  longer freezes for the life of the page. A failure in the moment between
  marking the update busy and handing it off left that one input marked busy
  forever, so it never animated again until a page reload. The mark is now
  released when the hand-off did not happen.

- Picking a page whose file is gone no longer blanks the deck. The page list
  can name a page whose file was deleted outside the app; choosing it cleared
  the deck to nothing. The deck now keeps its page and a message says the page
  could not be loaded.

- The comment field and the allow-media, allow-background and per-label
  toggles in the sidebar keep working after an error. A failure part way
  through updating one of these rows left its control silently dead until a
  restart: edits typed into it were dropped. The rows now rewire themselves
  whatever happens during the update.

- A background image on a Stream Deck + now lines up across the gaps between
  keys. The background was cut into key tiles at a spacing that did not match
  the device, so a line in the image jogged sideways at every key edge. The
  spacing was measured on the device and the tiles now follow it; other decks
  are unchanged.

- A Stream Deck Neo's two touch buttons no longer fire a random key. Their
  presses were run through the key-grid rotation map like grid keys, so a
  touch reached a real key's action or a broken position that varied with the
  deck's rotation. Those presses are now ignored, since the touch buttons have
  no action of their own to run.

- An import from Stream Deck UI that fails now shows the error and closes the
  dialog instead of freezing. A failure part way through, such as an unreadable
  source file, was logged and swallowed, so the progress bar sat at the start
  and the dialog never closed. The failure now reaches the dialog as a message
  and the dialog closes.

- A key that holds more than one action is recognised as a multi-action key
  again. The check counted the key's saved states instead of its actions, so a
  key with several states but one action each read as multi-action, and a key
  with two actions in a single state read as one. It now counts the actions on
  the key's own state.

- A deck that stops answering key presses while it stays plugged in now comes
  back on its own. A read error inside the Stream Deck library could end the
  thread that reports every press, turn and touch, which left the deck
  showing its page and ignoring every input until the app restarted. The app
  now watches that thread, takes the device handle back and opens it again
  when it dies, and repaints the deck. A device that keeps losing its reader
  is left alone after five attempts in a row: the app stops driving it, its
  screens keep the last picture they were given, and one line in the log says
  to replug it.

- A deck that answers nothing at all now takes one USB reset before the app
  leaves it alone. A deck can keep its place on the bus and refuse every
  connection, and until then only a replug brought it back: it stayed dead
  across restart after restart. The app now resets that one device the way
  the kernel does for a replug and tries once more, both for a deck that
  will not start up and for a deck whose input stopped and whose handle
  will not open again. It resets one device once, it never resets a device
  it cannot tell apart from a second deck of the same model, and where the
  app runs without access to the USB devices it says so in the log and asks
  for a replug as before.

- A failure while starting one input's periodic update no longer stops every
  animated action on that deck. The update loop ended on the first such
  failure and stayed stopped until the app restarted. It now reports the
  failure, names the input it came from, holds its log to one report every
  few seconds, and carries on with the rest of the deck.

- A dial that shows a plugin's own image or animation keeps it after the page
  reloads. Rebuilding the dial's states after a settings change wiped that
  image and never put it back, so the dial went blank until the page changed.

- A plugin can now set an image or animation on the SD+ touch strip behind the
  dials, and clearing a dial's image works. Both did nothing or raised an error
  before.

- A deck background that changes while the deck is drawing no longer drops the
  frame. The background tiles were read and written from several threads with
  no lock, so a change that landed mid-draw could tear the read and log an
  error instead of painting.

- A default page that cannot be built no longer blanks the deck. When the
  page a deck opens on resolved by name but failed to build, the deck was
  cleared to nothing; it now keeps the page it already shows.

- A state change sent right after a page switch, from the command line or over
  D-Bus, now applies reliably. The request used to race the page's input load
  and be rejected as out of range, or the app briefly froze while a page with
  a video background finished loading. The request now waits only for the new
  page's inputs to rebuild, so it is neither rejected nor blocked on the
  background.

- An action on a page whose saved file holds a damaged state key no longer
  stays dark and unresponsive. A state key that is not a number stopped the
  page's ready step half way, so the action never finished loading and ignored
  every later update for the life of the page. The app now steps past the bad
  key and finishes the load.

- A label or image set by a plugin on one page no longer appears on another
  page. A setter reached every deck that held the same key and state, even a
  deck showing a different page; it now writes only to the decks that currently
  show the page it was called on.

### Security

- A plugin install that another program on your desktop session asks for now
  waits for your answer. The app publishes an install-plugin control on the
  session bus, which is how the Install button of a "plugin missing"
  notification reaches it, and any other program on the session could use the
  same control. Such a request now raises a dialog that names the plugin, and
  installs nothing until you agree, so no program can install a plugin behind
  your back. The dialog comes before the store is contacted, so a request you
  refuse costs nothing, and one request is handled at a time. The store window,
  the first-run page and the "install the missing plugin" button never used
  that control and are unchanged.

- Installing a plugin from the store now asks before it runs the plugin's
  install steps, and runs them confined. A plugin can ship a setup step
  that runs on this computer at install time; one such step silently
  changed a system setting outside the app. The store now asks before
  running a plugin's install steps, and, where the bwrap sandbox tool is
  present, runs each step on a read-only system with access only to the
  plugin's own folder and no connection to the desktop session. Where
  bwrap is not available the step still runs, with the desktop session
  hidden from it as far as the environment allows; the prompt is the main
  protection there. A hung step is stopped instead of holding the install
  open, and declining an update keeps the working plugin in place. A new
  setting under Store, "Plugin install scripts" (ask, always or never),
  controls the prompt; the prompt appears for store installs, while an
  automatic update of a plugin you already installed runs its steps
  without asking unless you declined them before.
- Plugin backends now listen on the local machine only, and accept
  connections from the desktop session that started them. A plugin backend
  used to open a network port that any machine on the same network could
  reach, and anything running on the computer could connect to the app's
  plugin ports; a connection to one of those ports could run code as the
  user. The app now starts each backend on a loopback-only port, checks that
  a connecting program is the same user on the same machine, and refuses and
  shuts down a backend that is still reachable from the network.
- Importing pages now stays inside the pages directory. An imported file
  names its pages, and a page name that pointed at a location outside the
  pages folder used to send the write there and could overwrite an unrelated
  file. The import now keeps every write inside the pages folder and skips a
  page or a deck whose name points outside it, while a normal import lands as
  before.
- Deleting a page now stays inside the pages directory. The request that
  removes a page named the file to delete, and a name that pointed outside the
  pages folder could delete an unrelated file. The delete now keeps to the
  pages folder and refuses a name that points outside it, while removing a real
  page works as before.
- A log you share no longer leaks a credential that sits after a scheme word.
  When a log line held a secret such as `token: Token <secret>` or
  `api_key: Basic <credential>`, the redaction that hides secrets before you
  share a log removed only the scheme word and left the secret in place, and
  for some schemes it left the whole value. The secret after the scheme word
  is now removed, and a token or api-key header name is recognised with an
  `X-` prefix as well.
- A log you share no longer names the machines on your network. A plugin that
  talks to a Home Assistant instance or an MQTT broker logs the address it
  connects to, and a log you posted for help described your network to
  everyone who read it. A host name and an ip address in a log now read as
  `<host>` and `<ip>`, and so does the name of this computer. The scheme, the
  port and the path of a url stay, loopback addresses stay, and the public
  sites the app itself uses stay, such as GitHub, so a store or a connection
  problem is still diagnosable from the log.

### Fixed

- Quitting now hands the deck back reliably, so the next start finds it free.
  The app closed the device while the library still watched it for button
  presses, and that watcher could take the handle straight back, leaving the
  deck held by an app that had already gone. The reader is now stopped before
  every close, including the closes that follow a deck that fails to start
  up, and a deck released this way is not re-opened behind the app's back.
- Editing the size, position, label or background of a key, dial or
  touchscreen now keeps saving after the sidebar loads an input the deck does
  not carry. A row disconnected its value handler, looked the input up, and a
  lookup that returned early skipped the reconnect, so the row silently
  dropped every later edit. One such case then aborted the rest of the sidebar
  load, leaving the label, action and background editors blank until restart.
- A deck now shows its screen saver when the app starts into a session that
  is already locked. The lock branch had only ever engaged on a later lock
  or unlock, so an app launched behind the lock screen (autostart racing the
  login lock, a restart while locked, a launch from a remote shell) left the
  decks lit and interactive at their pages. Startup now reads the current
  lock state once and locks at once when the session reads locked.
- Searching for an icon, a wallpaper or a custom asset now matches on the
  words of the name. A search compared the whole name against the whole
  query, so a long name lost for its length alone: "battery" answered with
  acer and afterpay while it hid battery-charging-outline. The search now
  keeps a name that holds every word typed, and lists the closest first, so
  the words may be typed in any order, a dash, an underscore or a dot reads
  the same as a space, and "wifi" finds a pack that writes it wi-fi. It
  matches the words as they are typed, so a misspelt word such as "batery"
  now finds nothing, where the old scoring still offered battery.
- The search box on the icon pack, wallpaper pack and SD+ bar wallpaper pack
  pages now filters the packs it shows. It accepted text and changed
  nothing.
- An asset search now waits a little longer for the typing to pause before it
  filters the grid, because a pass sorts a whole icon pack. Emptying the box
  still brings the grid back at once.
- A page that switches itself in when a window comes to the front now works
  from one pattern. A rule set up by filling in the window title alone, or the
  window class alone, never fired: the field that was never filled in was
  absent from the rule, and an absent field was read as one that matches
  nothing. Such a field now matches every window, while a rule that carries
  neither field stays inert instead of matching everything, so a rule being
  typed cannot take a deck over. Editing a rule also applies it to the window
  in front there and then, instead of waiting for the next window change, and
  the title and class fields keep what was typed in them when the focus moves
  on or the window closes.
- The plugin store reads both store catalog formats, the old per-version
  commit map and the new single-commit form, so a store that publishes the
  new format still lists, installs and updates its assets instead of showing
  an empty page. A store entry that names a branch now resolves that branch
  everywhere; a broken pin beside it no longer hides the entry from the
  store window while updates keep following the branch.
- The search box and the image and video buttons on the Custom Assets tab now
  filter what the grid shows, and the grid lists assets by name. The tab
  handed the grid its search filter and its sort order under names the grid
  already used for something else, so both were dropped: every custom asset
  stayed on screen, in storage order, whatever was typed or toggled.
- An icon pack, wallpaper pack or SD+ bar wallpaper pack whose manifest leaves
  out its asset folder no longer stops every pack of that kind from loading.
  One such pack used to fail the whole list, so the asset chooser showed none
  of them; now that pack alone is reported as unusable. A manifest that leaves
  out its thumbnail used to leave the chooser loading forever, and now loads
  the pack without a thumbnail.
- Tray menus now answer a host that asks for a single menu property. The
  property was looked up in the wrong place and the reply was built in the
  wrong shape, so the host got no reply at all and waited for its timeout.
- Onboarding no longer fails to report which recommended plugins could not be
  installed. A plugin whose store record carries no name put nothing in the
  list, and building the summary message then failed.
- A plugin's settings window now opens when one of the plugin's custom icons
  has had its image file deleted. The window failed to build at all, so none
  of that plugin's settings could be reached.
- The info button on an icon inside an icon pack now works when the pack
  ships no attribution for that icon. The dialog failed to open instead.
- Reverting a background colour no longer leaves the colour picker unable to
  save. A failure part-way through the revert used to disconnect the row's
  signals and never reconnect them, so later colour changes on that row were
  quietly dropped.
- The deck pickers in the page editor now open when one deck cannot be
  identified. A deck whose serial number could not be read used to stop the
  whole list from building, so neither the default-page row nor the
  auto-change row could pick a deck.
- Changing a page's brightness, background or screensaver override now
  reaches every connected deck. A deck that had not loaded a page yet used to
  stop the update, so any deck listed after it kept the old setting until the
  next page load.
- Opening the details of a store icon pack, wallpaper or SD+ bar wallpaper no
  longer fails when the asset carries translated licence descriptions. The
  details panel raised an error and stayed on the previous asset's values;
  now it shows the description for the active language.
- The About dialog of a plugin now opens when the plugin's manifest omits its
  name, version or repository. It used to fail silently and show nothing.
- Editing or resetting a custom plugin icon whose image file has since been
  deleted no longer fails. The icon editor now reports that there is no image
  behind the entry instead of doing nothing.
- The store no longer marks an icon pack or wallpaper as installed when its
  download actually failed. An install button used to flip to "installed" over
  a pack that a 404, a rejected file, or an unreachable store had never
  written; now a failed install leaves the button as it was and reports the
  failure, so it can be retried.
- Installing a plugin from a missing-action row or from the onboarding
  recommendations no longer hangs when the store cannot be reached. The button
  used to spin forever on its "installing" label with no way back; now an
  unreachable store surfaces the same install-failed state as any other
  failure, and the row can be retried.
- Screensaver and brightness settings now show the values the deck actually
  uses. A page's screensaver brightness slider read 75 for a screensaver that
  had never been given one, while the deck dimmed to 30; a deck's own settings
  page offered 50 as the brightness for a deck that was running at 75. Decks
  that already have these values chosen keep them exactly as they are.
- Opening a deck's settings no longer changes or rewrites its configuration.
  The page used to fill in every setting the deck had never been given and
  save the result — which, for brightness, also dimmed the deck on the spot,
  and froze that day's defaults into the deck's configuration so that later
  improvements to them never reached it.
- Deck-level background media without an explicit loop setting now loops,
  instead of playing once and holding its last frame for as long as the page
  is up. Page-level background media is unchanged: it still plays once by
  default.
- A deck plugged in while the session is locked now shows the screensaver it
  is configured with, at the brightness it is configured with, instead of a
  blank panel at a brightness nothing had chosen.
- Opening the settings of a deck that has been rotated no longer re-saves and
  re-applies that rotation, with the full page reload that comes with it.
- Selecting pages in the page manager no longer makes the page editor's
  screensaver brightness slider write its own displayed value back to each
  page it visits, and reapply it to the deck.
- A plugin whose settings file cannot be read no longer breaks that plugin's
  settings window. Choosing an icon or a colour there failed outright when the
  file was present but unreadable — a permissions change, a disconnected drive
  — instead of treating it as empty the way every other read of that file
  already did.
- Changing a default font in the settings window no longer reverts other
  general settings that changed while that window was open. The font was
  saved correctly, and then the window's picture of the settings file — taken
  when it opened — was written on top of it, putting back the hold time and
  the rest as they had been at that moment.
- A deck plugged in after Deckard has started now appears on the D-Bus
  interface, and a deck that is unplugged disappears from it. Only the decks
  present at launch were ever published, so scripts driving Deckard over
  D-Bus could not see a deck plugged in later — including the common case of
  starting with the session, before the deck is ready — while a deck that had
  been removed stayed on the interface, still answering calls.
- The active page name a deck reports over D-Bus is now correct from the
  moment it appears there, instead of reading empty until something changed
  the page.
- Asking a deck over the D-Bus interface to show the page it is already
  showing now does nothing, instead of reloading the deck. Scripts that set a
  page on every event — a window rule, a home-automation trigger — made the
  deck re-render all of its keys each time.
- Every `--change-page` and `--change-state` on one command line now takes
  effect. When Deckard was already running, only the first request was sent to
  it: a command carrying several page changes moved one deck and left the
  others alone, and any state change on a command that also changed a page was
  dropped entirely.
- `--change-page` and `--change-state` now report failures in the terminal you
  typed them in, and exit non-zero, instead of leaving the reason in the
  running instance's log where nothing showed it to you. Requests are also no
  longer pre-rejected against limits the CLI invented (coordinates above 10,
  states above 20): a large deck's real coordinates and state numbers now
  reach the running instance, which checks them against the device itself.
- A page that cannot be loaded no longer blanks the deck while reporting
  success. Asking for a page whose file is present but unusable cleared every
  key and said the switch had worked; the deck now keeps the page it was
  showing and the request explains itself.
- Switching to a page while several pages are already cached no longer lands
  you on a dead one. With enough pages open the app could discard a page in
  the moment between picking it and showing it, leaving a deck whose keys
  looked right but did nothing until the page was loaded again — and leaving
  a second, competing copy of that page behind.
- Page edits made in different places at the same time no longer overwrite
  each other. Changing a page's settings — a screensaver, brightness,
  background or window-rule override — while a plugin or a key edit was
  saving the same page could silently drop one of the two changes, from the
  file and from the running app, so a setting you had just made would be back
  to its old value the next time you looked.
- Window-based automatic page switching now works on desktops that report a
  colon-joined `XDG_CURRENT_DESKTOP`, such as `ubuntu:GNOME` — window
  grabbing matched only single-name desktops and stayed silently disabled
  everywhere else.
- An idle deck showing the screensaver no longer repaints every key, dial and
  touchscreen once a second; a still screensaver now costs no per-second work
  at all. Recovering the picture after a dropped device write is handled by
  the existing repaint retry instead, which also covers a rare blank deck on
  screensaver entry that the per-second repaint had been masking.
- A deck that fails to connect no longer leaves background work running for
  the rest of the session. Every failed attempt — a deck whose USB link flakes
  while the session is starting, or one already claimed by another instance —
  stranded the threads that had been started for it, including one still
  trying to draw to the deck it never got. Repeated attempts piled them up, so
  a deck that would not come up cost idle CPU and memory until Deckard was
  restarted.
- Launching Deckard while it is still starting up now waits for that startup
  and brings the window up when it finishes, instead of giving up after ten
  seconds and exiting with an error — which also lost a `--change-page` sent
  in that window. A startup slower than the session bus's own call timeout
  still outlasts the wait.
- A page or state change given to a launch that lost the startup race is no
  longer dropped. Two launches at the same moment leave one of them handing
  over to the other, and the requests it had already taken went with it —
  nothing applied, nothing said, and a successful exit code.
- The message shown when the running Deckard does not answer `--change-page`
  or `--change-state` now names what it can actually mean. It offered a
  startup that had not finished as the likely explanation, which stopped being
  possible when the app started publishing its interface before taking its bus
  name; it now names the two things that remain — an older build, and an
  instance that is shutting down — with what to do about each.
- The list of controllers on the D-Bus interface now matches the objects a
  client can address. It was read from the decks the app had, so during the
  moment between a deck being added and its object appearing it named a deck
  whose object was not there yet — and a client that composed a path from that
  list got an error for a deck that was plainly plugged in.
- Asking to change a page on a deck when none are connected now says that the
  app may still be starting, rather than only that nothing is connected: the
  interface answers from the moment Deckard takes its bus name, which is
  before it has opened any deck.
- A damaged custom-asset library file no longer prevents Deckard from
  starting. It was read while the app was still building itself, so an
  unreadable one stopped the app before any window appeared, with nothing on
  screen to say why. Deckard now starts with an empty custom-asset library and
  says so in the log; the damaged file is renamed aside and kept next to the
  new empty one, so nothing is thrown away, and the asset files themselves are
  untouched.
- Importing a StreamDeck UI profile now updates the deck settings the rest of
  the app is reading. The import wrote its brightness and screensaver
  preferences straight to the file, so anything that had already read that
  deck — the deck itself included — could go on showing the settings from
  before the import until something else caused them to be read again.
- The AUR package no longer ships development files under `/opt/deckard`. A
  built package could carry linter caches picked up from the build directory,
  along with CI configuration, devcontainer files, and the packaging recipe
  itself; the installed tree now holds only the application and its bundled
  environment.

### Changed

- A second launch now hands off to the running Deckard through the application
  framework itself, instead of the app deciding for itself whether one was
  already running. Two launches starting at the same moment — a session
  autostart alongside a restored session — can no longer both reach the decks.
- `--close-running` that does not manage to close the running Deckard now
  exits with an error instead of reporting success, and it waits for the
  instance to actually let go rather than for a fixed five seconds. Against an
  instance that is itself still starting up it waits until that instance can
  answer before asking it to quit, so the two can no longer leave you with
  nothing running at all.
- Dragging a key onto another position now changes the page in one step. The
  two keys used to be exchanged one at a time with a save around each stage,
  so the page file was written three times for one drag and a write landing
  mid-swap could put one key's actions under both positions until the next
  save corrected it. Choosing an icon for a key no longer saves the page a
  second time on top of the save that setting it already made.
- Page edits are written in the background about a second after the last
  change, instead of once per keystroke. Typing a label used to write the
  whole page file — with two disk syncs — for every character, on the same
  thread that draws the window. The page is still written immediately when
  you switch page, close a deck or quit the app, and whenever anything reads
  the file, so what you see and what is exported or backed up is always
  current. The trade: a crash or power cut can now cost the last second of
  edits — up to five while you are typing continuously — where before it
  could cost none.
- The active window is no longer watched in the background unless a page
  actually uses a window auto-change rule. Previously every session polled
  the foreground window continuously — several helper processes a second on
  X11 and KDE — whether or not any page asked for it. The watcher now starts
  the moment the first rule is enabled and stops when the last one is removed.
  Consequently the D-Bus `ForegroundWindow` property now tracks the desktop
  only while window rules are in use; it can still be set from outside at any
  time via `NotifyForegroundWindow`.
- Startup is faster and much quieter on the network: the automatic store
  update check now reads the store catalogue plus what is already installed,
  instead of downloading a thumbnail, a manifest and an attribution file for
  every asset in the store — including the ones you never installed. Opening
  the store window still loads the full listing with images.
- Log files are now pruned automatically (the ten most recent rotations are
  kept) and default verbosity is lower — files record debug level and up, the
  console info and up. Set `SC_LOG_TRACE=1` to restore full trace logging on
  every sink for diagnosis.

## [0.2.1] - 2026-08-09

### Fixed

- Remote decks no longer misreport touch capability; touchscreen strip frames
  are no longer rendered and encoded for decks that cannot display them.
- Window grabbing survives an unset `XDG_CURRENT_DESKTOP` on X11 — previously
  a startup crash left it silently disabled for the whole session.
- Importing a StreamDeck-UI profile now rejects hotkeys carrying delay tokens
  with a clear message instead of silently dropping the whole hotkey.
- Outdated-action rows in the sidebar render again instead of failing on
  construction.
- Unplugging a deck mid-render no longer crashes the media tick on image-size
  lookups, and media loads skip cleanly when a deck closes during page load.
- Store items with missing or corrupt manifests are dropped with a log line
  instead of crashing catalog preparation workers, and downloads without a
  resolvable ref fail up front instead of staging junk.
- Notifications, plugin installs and DBus page/state changes degrade
  gracefully during startup and teardown instead of raising inside idle
  callbacks.

### Changed

- The entire tree now type-checks clean under mypy and the CI type-check lane
  is a blocking gate; roughly fifty latent defects surfaced by the typing work
  were fixed along the way.

## [0.2.0] - 2026-08-09

### Fixed

- Mutable default arguments across 8 sites, two of them on the plugin-facing
  API (`ActionHolder.action_support`, `ActionCore.set_background_color`): the
  shared default object leaked mutations between unrelated callers for the
  lifetime of the process.

### Changed
- test: deflake fair_lock and native_tile_cache — measure the right intervals, settle the right threads
- fix(action-core): gate the default on_update's compat on_ready on on_ready_finished
- fix(persistence): quarantine corrupt plugin JSON + bound .corrupt retention at every quarantine site
- refactor(asset-manager): one chooser base pair; flow boxes built on the main thread
- fix: backend bug batch — screensaver loop default, store ref fail-hard, redaction idempotence
- docs: retire the false self-heal claim; stamp the landed disposition on the presenter plan
- feat(hooks): SC_NO_ERROR_HOOKS kill-switch + per-site rate limiting
- fix: UI/asset bug batch — permission window arity, label font_name, URL-import sentinel, CSS alpha scale, GNOME ext uuid
- refactor(settings): debounce the font-row page-reload storms
- perf(store): defer read-clock index writes behind a debounced flush
- perf(labels): static-label raster cache — record/replay the draw.text mask blits
- perf(gif): cap the KeyGIF working set — opaque GIFs via the mp4 tile registry, budgeted alpha path
- fix(media): GIF transparency + per-frame timing outside KeyGIF
- fix: upstream-derived small fixes — store tag refs, corrupt-asset gate, Gio data-path open
- feat(lockscreen): systemd-logind fallback lock detector (Niri/Sway/river)
- perf: key/touchscreen mirror PIL→pixbuf off the GTK loop; batch set_main_error
- fix(app): skip destroying a never-realized main window at quit (GTK unrealized-dispose abort)
- fix: subprocess hygiene — list-argv backend launch, de-forked run_command, Gio URL opening
- perf(store): shared requests.Session with 429 retry/backoff + unified download helper
- fix(app): route SIGTERM/SIGHUP through on_quit so logout TERM terminates plugin backends
- refactor: concurrency-idiom cleanups -- sync StoreBackend, join waits, timer-wheel stragglers
- fix(store): transactional installs -- stage, validate, swap, delete old last
- perf: frame-identity native tile cache for background video
- perf: fair FIFO transport lock; retire 20Hz write cap defaults
- fix(ui): page change refreshes the sidebar again
- fix(logging): scrub faulthandler.log in place to keep live fds valid
- fix(ui): bind deck-stack child to its controller by identity, not serial name
- fix(app): never rebuild MainWindow on remote activation
- fix(boot): claim the single-instance lock atomically before touching decks

## [0.1.0] - 2026-07-14

### Added

- First Deckard release: an installable flatpak bundle is built and published
  as a release asset on every `vX.Y.Z` tag.
- Native Arch-family package (`deckard-git`) for the AUR, alongside the flatpak.
- Native installs run as the `deckard` command and store their data under
  `$XDG_DATA_HOME/deckard` (`~/.local/share/deckard`), migrated automatically
  from the previous `~/.var/app` location.
- About dialog shows the Deckard fork release version (from the `VERSION`
  file); the upstream StreamController base is noted in the About comments.
