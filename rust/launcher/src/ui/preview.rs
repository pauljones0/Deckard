//! Fixed logical preview sizes, independent of the device texture's resolution.
use super::*;
use gtk::subclass::prelude::*;

mod imp {
    use super::*;
    #[derive(Default)]
    pub struct PreviewImage {
        pub size: Cell<(i32, i32)>,
        pub paintable: RefCell<Option<gdk::Paintable>>,
    }
    #[glib::object_subclass]
    impl ObjectSubclass for PreviewImage {
        const NAME: &'static str = "DeckardPreviewImage";
        type Type = super::PreviewImage;
        type ParentType = gtk::Widget;
    }
    impl ObjectImpl for PreviewImage {}
    impl WidgetImpl for PreviewImage {
        fn measure(&self, orientation: gtk::Orientation, _for_size: i32) -> (i32, i32, i32, i32) {
            let size = self.size.get();
            let side = if orientation == gtk::Orientation::Horizontal {
                size.0
            } else {
                size.1
            };
            (side, side, -1, -1)
        }
        fn snapshot(&self, snapshot: &gtk::Snapshot) {
            let Some(paintable) = self.paintable.borrow().clone() else {
                return;
            };
            let obj = self.obj();
            let (width, height) = (obj.width() as f64, obj.height() as f64);
            let aspect = paintable.intrinsic_aspect_ratio();
            let (w, h) = if aspect > 0. {
                if width / height > aspect {
                    (height * aspect, height)
                } else {
                    (width, width / aspect)
                }
            } else {
                (width, height)
            };
            snapshot.save();
            snapshot.translate(&gtk::graphene::Point::new(
                ((width - w) / 2.) as f32,
                ((height - h) / 2.) as f32,
            ));
            let rect = gtk::graphene::Rect::new(0., 0., w as f32, h as f32);
            snapshot.push_rounded_clip(&gtk::gsk::RoundedRect::from_rect(rect, 10.));
            paintable.snapshot(snapshot, w, h);
            snapshot.pop();
            snapshot.restore();
        }
    }
}
glib::wrapper! {
    pub struct PreviewImage(ObjectSubclass<imp::PreviewImage>)
        @extends gtk::Widget,
        @implements gtk::Accessible, gtk::Buildable, gtk::ConstraintTarget;
}
impl PreviewImage {
    pub fn new(width: i32, height: i32) -> Self {
        let obj: Self = glib::Object::new();
        obj.imp().size.set((width, height));
        obj.set_size_request(width, height);
        obj
    }
    pub fn paintable(&self) -> Option<gdk::Paintable> {
        self.imp().paintable.borrow().clone()
    }
    pub fn set_paintable(&self, paintable: Option<&impl IsA<gdk::Paintable>>) {
        let paintable = paintable.map(|p| p.clone().upcast::<gdk::Paintable>());
        if *self.imp().paintable.borrow() != paintable {
            *self.imp().paintable.borrow_mut() = paintable;
            self.queue_draw();
        }
    }
}
