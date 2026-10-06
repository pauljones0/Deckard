use super::*;

pub(super) fn clear(container: &gtk::Box) {
    while let Some(child) = container.first_child() {
        container.remove(&child);
    }
}
pub(super) fn margins(widget: &impl IsA<gtk::Widget>, margin: i32) {
    widget.set_margin_start(margin);
    widget.set_margin_end(margin);
    widget.set_margin_top(margin);
    widget.set_margin_bottom(margin);
}
pub(super) fn text(title: &str, value: &str, change: impl Fn(String) + 'static) -> adw::EntryRow {
    let row = adw::EntryRow::builder().title(title).text(value).build();
    row.connect_changed(move |row| change(row.text().to_string()));
    row
}
pub(super) fn number(
    title: &str,
    value: f64,
    min: f64,
    max: f64,
    step: f64,
    change: impl Fn(f64) + 'static,
) -> adw::ActionRow {
    let row = adw::ActionRow::builder().title(title).build();
    let spin = gtk::SpinButton::with_range(min, max, step);
    spin.set_digits(if step < 1. { 2 } else { 0 });
    spin.set_value(value);
    spin.set_valign(gtk::Align::Center);
    row.add_suffix(&spin);
    row.set_activatable_widget(Some(&spin));
    spin.connect_value_changed(move |spin| change(spin.value()));
    row
}
pub(super) fn toggle(title: &str, value: bool, change: impl Fn(bool) + 'static) -> adw::SwitchRow {
    let row = adw::SwitchRow::builder().title(title).active(value).build();
    row.connect_active_notify(move |row| change(row.is_active()));
    row
}
pub(super) fn choice(
    title: &str,
    options: &[&str],
    value: &str,
    change: impl Fn(String) + 'static,
) -> adw::ComboRow {
    let mut options: Vec<String> = options.iter().map(|s| (*s).into()).collect();
    if !value.is_empty() && !options.iter().any(|s| s == value) {
        options.push(value.into());
    }
    let refs = options.iter().map(String::as_str).collect::<Vec<_>>();
    let model = gtk::StringList::new(&refs);
    let row = adw::ComboRow::builder()
        .title(title)
        .model(&model)
        .selected(options.iter().position(|s| s == value).unwrap_or(0) as u32)
        .build();
    row.connect_selected_notify(move |row| {
        if let Some(item) = row.selected_item().and_downcast::<gtk::StringObject>() {
            change(item.string().to_string());
        }
    });
    row
}
pub(super) fn color(
    title: &str,
    value: [u8; 4],
    change: impl Fn(Value) + 'static,
) -> adw::ActionRow {
    let dialog = gtk::ColorDialog::builder().with_alpha(true).build();
    let button = gtk::ColorDialogButton::new(Some(dialog));
    button.set_rgba(&gdk::RGBA::new(
        value[0] as f32 / 255.,
        value[1] as f32 / 255.,
        value[2] as f32 / 255.,
        value[3] as f32 / 255.,
    ));
    button.set_valign(gtk::Align::Center);
    let row = adw::ActionRow::builder().title(title).build();
    row.add_suffix(&button);
    row.set_activatable_widget(Some(&button));
    button.connect_rgba_notify(move |button| {
        let c = button.rgba();
        change(json!(
            [c.red(), c.green(), c.blue(), c.alpha()].map(|c| (c * 255.).round() as u8)
        ));
    });
    row
}
pub(super) fn button(title: &str, icon: &str, callback: impl Fn() + 'static) -> gtk::Button {
    let button = if title.is_empty() {
        gtk::Button::from_icon_name(icon)
    } else if icon.is_empty() {
        gtk::Button::with_label(title)
    } else {
        let content = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        content.set_halign(gtk::Align::Center);
        content.append(&gtk::Image::from_icon_name(icon));
        content.append(&gtk::Label::new(Some(title)));
        gtk::Button::builder()
            .child(&content)
            .tooltip_text(title)
            .build()
    };
    if !title.is_empty() {
        button.set_widget_name(title);
    }
    button.connect_clicked(move |_| callback());
    button
}
pub(super) fn expander(title: &str, subtitle: &str) -> (adw::PreferencesGroup, adw::ExpanderRow) {
    let group = adw::PreferencesGroup::new();
    let row = adw::ExpanderRow::builder()
        .title(title)
        .subtitle(subtitle)
        .build();
    group.add(&row);
    (group, row)
}
pub(super) fn file_row(
    parent: &adw::ApplicationWindow,
    title: &str,
    value: &str,
    change: impl Fn(String) + 'static,
) -> adw::EntryRow {
    let change = Rc::new(change);
    let callback = change.clone();
    let row = text(title, value, move |v| callback(v));
    let browse = gtk::Button::from_icon_name("folder-open-symbolic");
    browse.add_css_class("flat");
    browse.set_valign(gtk::Align::Center);
    row.add_suffix(&browse);
    let weak_parent = parent.downgrade();
    let weak_row = row.downgrade();
    let title = title.to_owned();
    browse.connect_clicked(move |_| {
        let Some(parent) = weak_parent.upgrade() else {
            return;
        };
        let row = weak_row.clone();
        let dialog = gtk::FileDialog::builder()
            .title(title.to_owned())
            .modal(true)
            .build();
        dialog.open(Some(&parent), None::<&gio::Cancellable>, move |result| {
            if let (Ok(file), Some(row)) = (result, row.upgrade())
                && let Some(path) = file.path()
            {
                row.set_text(&path.to_string_lossy());
            }
        });
    });
    row
}

pub(super) fn get_path<'a>(value: &'a Value, path: &[&str]) -> &'a Value {
    path.iter().fold(value, |v, key| &v[*key])
}
