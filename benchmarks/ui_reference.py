#!/usr/bin/env python3
"""Capture the real, frozen Python reference; developer tooling only."""
import pathlib,runpy,sys,time,os,json
from gi.repository import GLib
source=pathlib.Path(sys.argv[1]).resolve();capture=pathlib.Path(sys.argv[2]).resolve()
sys.path.insert(0,str(source));sys.argv=[str(source/'main.py'),*sys.argv[3:]]
start=time.monotonic()
scene_done=False
scene_time=0
def walk(w):
 yield w
 child=w.get_first_child()
 while child is not None:
  yield from walk(child)
  child=child.get_next_sibling()
def case_debug():return os.environ.get('DECKARD_UI_CAPTURE_CASE')=='assets'
def take():
 global scene_done,scene_time
 import gi
 gi.require_version('Gtk','4.0')
 from gi.repository import Gtk,Adw
 windows=list(Gtk.Window.get_toplevels())
 main=next((window for window in windows if type(window).__name__=='MainWindow' and (scene_done or window.get_visible())),None)
 if main is None or time.monotonic()-start<8:return GLib.SOURCE_CONTINUE
 case=os.environ.get('DECKARD_UI_CAPTURE_CASE','')
 if not scene_done:
  scene_done=True;scene_time=time.monotonic()
  import globals as gl
  if case.startswith('settings'):
   main.menu_button.on_open_settings(None,None)
   if '-' in case:
    for w in Gtk.Window.get_toplevels():
     if w.get_title()=='Settings':
      wanted='UI' if case=='settings-ui' else case.split('-',1)[1].title()
      for widget in walk(w):
       if isinstance(widget,Adw.PreferencesPage) and widget.get_title()==wanted:w.set_visible_page(widget);break
  elif case.startswith('assets'):
   gl.app.let_user_select_asset(default_path=None,callback_func=lambda *a:None)
   if '-' in case:gl.app.asset_manager.asset_chooser.set_visible_child_name(case.split('-',1)[1])
  elif case=='pages':main.sidebar.page_selector.on_click_open_page_settings(None)
  elif case=='deck-settings':main.deck_settings_button.emit('clicked')
  elif case in ['chooser','chooser-populated']:
   from src.backend.DeckManagement.InputIdentifier import Input
   main.sidebar.let_user_select_action(lambda *a:None,Input.Key('0x0'))
  elif case=='action':
   for widget in walk(main):
    if hasattr(widget,'action_object') and hasattr(widget,'on_click'):widget.on_click(None);break
  elif case=='labels-detail':
   for widget in walk(main):
    if isinstance(widget,Adw.ExpanderRow) and widget.get_title()=='Labels':widget.set_expanded(True)
   GLib.timeout_add(250,lambda: (main.sidebar.key_editor.scrolled_window.get_vadjustment().set_value(400),False)[1])
  elif case=='dial':
   from src.backend.DeckManagement.InputIdentifier import Input
   for widget in walk(main):
    if type(widget).__name__=='Dial' and getattr(widget,'identifier',None)==Input.Dial('0'):widget.on_click(type('Gesture',(),{'get_current_button':lambda s:1})(),1,0,0);break
  elif case=='touchscreen':
   from src.backend.DeckManagement.InputIdentifier import Input
   for widget in walk(main):
    if type(widget).__name__=='ScreenBar':widget.on_click(type('Gesture',(),{'get_current_button':lambda s:1})(),1,0,0);break
  elif case=='page-selector':main.sidebar.page_selector.page_button.popup()
  elif case=='labels':
   for widget in walk(main):
    if isinstance(widget,Adw.ExpanderRow) and widget.get_title() in ['Labels','Layout','Background']:widget.set_expanded(True)
  print("Scene complete",case,flush=True)
  return GLib.SOURCE_CONTINUE
 if time.monotonic()-scene_time<3:return GLib.SOURCE_CONTINUE
 target=next((w for w in Gtk.Window.get_toplevels() if w.get_visible() and w.get_title()==('Settings' if case.startswith('settings') else 'Asset Manager' if case.startswith('assets') else {'pages':'Page Manager'}.get(case,''))),main)
 paintable=Gtk.WidgetPaintable.new(target);snapshot=Gtk.Snapshot.new()
 paintable.snapshot(snapshot,target.get_width(),target.get_height())
 node=snapshot.to_node()
 if node is None:
  child=target.get_first_child()
  if child is None:return GLib.SOURCE_CONTINUE
  snapshot=Gtk.Snapshot.new();Gtk.WidgetPaintable.new(child).snapshot(snapshot,child.get_width(),child.get_height());node=snapshot.to_node()
  if node is None:
   snapshot=Gtk.Snapshot.new();child=target.get_first_child()
   while child is not None:
    target.snapshot_child(child,snapshot);child=child.get_next_sibling()
   node=snapshot.to_node()
   print('Capture node',case,type(node),target.get_title(),flush=True)
   if node is None:return GLib.SOURCE_CONTINUE
 texture=target.get_renderer().render_texture(node,None)
 assert texture.save_to_png(str(capture))
 rows=[]
 for w in walk(main):
  if hasattr(w,'pixbuf') and w.pixbuf is not None and hasattr(w,'identifier'):
   print('Mirror pixel',str(w.identifier),list(w.pixbuf.read_pixel_bytes().get_data()[:4]),flush=True)
 for widget in walk(target):
  if not widget.get_mapped():continue
  ok,bounds=widget.compute_bounds(target)
  title=widget.get_label() if isinstance(widget,(Gtk.Label,Gtk.Button)) else widget.get_title() if isinstance(widget,Adw.PreferencesRow) else None
  rows.append(dict(type=widget.__gtype__.name,title=title,css=widget.get_css_classes(),bounds=[bounds.get_x(),bounds.get_y(),bounds.get_width(),bounds.get_height()] if ok else None))
 capture.with_suffix('.json').write_text(json.dumps(rows,indent=2))
 print('Legacy GTK screenshot saved',capture,flush=True)
 return GLib.SOURCE_REMOVE
GLib.timeout_add(1000,take)
runpy.run_path(str(source/'main.py'),run_name='__main__')
