"""
Zone Editor for the WhatsApp label-verification Lambda.

Load a label photo, drag out validation zones on it, fill in each zone's
validation parameters, and export the spec's {spec_code}_config.json in the
exact format validate_label_layout() in lambda_function.py expects.

Run: python zone_editor.py
"""
import json
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk


class ZoneDialog(tk.Toplevel):
    """Popup form for entering/editing a single zone's validation parameters."""

    def __init__(self, parent, zone=None):
        super().__init__(parent)
        self.title("Zone Parameters")
        self.result = None
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        zone = zone or {}
        row = 0

        tk.Label(self, text="Name:").grid(row=row, column=0, sticky="e", padx=5, pady=3)
        self.name_var = tk.StringVar(value=zone.get("name", ""))
        tk.Entry(self, textvariable=self.name_var, width=32).grid(row=row, column=1, columnspan=2, sticky="w", padx=5, pady=3)

        row += 1
        tk.Label(self, text="Detection engine:").grid(row=row, column=0, sticky="e", padx=5, pady=3)
        self.engine_var = tk.StringVar(value=self._initial_engine(zone))
        engine_frame = tk.Frame(self)
        engine_frame.grid(row=row, column=1, columnspan=2, sticky="w")
        for label, value in [("Rekognition (default)", "rekognition"), ("Textract", "textract"), ("Vision LLM (Gemini)", "vision_llm")]:
            tk.Radiobutton(engine_frame, text=label, variable=self.engine_var, value=value).pack(anchor="w")

        row += 1
        tk.Frame(self, height=1, bg="gray70").grid(row=row, column=0, columnspan=3, sticky="ew", padx=5, pady=6)

        row += 1
        self.pattern_enabled = tk.BooleanVar(value=bool(zone.get("expected_pattern")))
        tk.Checkbutton(self, text="Regex pattern:", variable=self.pattern_enabled).grid(row=row, column=0, sticky="e", padx=5, pady=3)
        self.pattern_var = tk.StringVar(value=zone.get("expected_pattern", ""))
        tk.Entry(self, textvariable=self.pattern_var, width=32).grid(row=row, column=1, columnspan=2, sticky="w", padx=5, pady=3)

        row += 1
        self.compare_var = tk.BooleanVar(value=zone.get("compare_to_reference", False))
        tk.Checkbutton(self, text="Compare to reference label (fuzzy match)", variable=self.compare_var).grid(
            row=row, column=0, columnspan=3, sticky="w", padx=5, pady=3
        )

        row += 1
        self.token_enabled = tk.BooleanVar(value=zone.get("expected_token_count") is not None)
        tk.Checkbutton(self, text="Expected token count:", variable=self.token_enabled).grid(row=row, column=0, sticky="e", padx=5, pady=3)
        count_frame = tk.Frame(self)
        count_frame.grid(row=row, column=1, columnspan=2, sticky="w")
        self.token_count_var = tk.StringVar(
            value=str(zone["expected_token_count"]) if zone.get("expected_token_count") is not None else ""
        )
        tk.Entry(count_frame, textvariable=self.token_count_var, width=6).pack(side="left")
        tk.Label(count_frame, text="  Tolerance:").pack(side="left")
        self.tolerance_var = tk.StringVar(value=str(zone.get("token_count_tolerance", 0)))
        tk.Entry(count_frame, textvariable=self.tolerance_var, width=5).pack(side="left")

        row += 1
        btn_frame = tk.Frame(self)
        btn_frame.grid(row=row, column=0, columnspan=3, pady=10)
        tk.Button(btn_frame, text="Save", command=self._on_save, width=10).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Cancel", command=self.destroy, width=10).pack(side="left", padx=5)

        self.bind("<Return>", lambda e: self._on_save())
        self.bind("<Escape>", lambda e: self.destroy())

    @staticmethod
    def _initial_engine(zone):
        if zone.get("use_vision_llm"):
            return "vision_llm"
        if zone.get("use_textract"):
            return "textract"
        return "rekognition"

    def _on_save(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Missing name", "Zone name is required.", parent=self)
            return

        result = {"name": name}

        engine = self.engine_var.get()
        if engine == "textract":
            result["use_textract"] = True
        elif engine == "vision_llm":
            result["use_vision_llm"] = True

        if self.pattern_enabled.get():
            pattern = self.pattern_var.get().strip()
            if not pattern:
                messagebox.showerror("Missing pattern", "Enter a regex pattern or uncheck it.", parent=self)
                return
            result["expected_pattern"] = pattern

        if self.compare_var.get():
            result["compare_to_reference"] = True

        if self.token_enabled.get():
            try:
                count = int(self.token_count_var.get())
            except ValueError:
                messagebox.showerror("Invalid count", "Expected token count must be a whole number.", parent=self)
                return
            result["expected_token_count"] = count
            try:
                tolerance = int(self.tolerance_var.get() or 0)
            except ValueError:
                tolerance = 0
            if tolerance:
                result["token_count_tolerance"] = tolerance

        self.result = result
        self.destroy()


class ZoneEditorApp(tk.Tk):
    MAX_DISPLAY_SIZE = (900, 750)

    def __init__(self):
        super().__init__()
        self.title("WhatsApp Label Zone Editor")

        self.image = None
        self.tk_image = None
        self.scale = 1.0
        self.zones = []
        self.rect_start = None
        self.current_rect_id = None

        self._build_ui()

    def _build_ui(self):
        toolbar = tk.Frame(self)
        toolbar.pack(side="top", fill="x")
        tk.Button(toolbar, text="Load Image", command=self.load_image).pack(side="left", padx=3, pady=3)
        tk.Button(toolbar, text="Load Existing Config", command=self.load_config).pack(side="left", padx=3, pady=3)
        tk.Button(toolbar, text="Edit Selected Zone", command=self.edit_selected_zone).pack(side="left", padx=3, pady=3)
        tk.Button(toolbar, text="Delete Selected Zone", command=self.delete_selected_zone).pack(side="left", padx=3, pady=3)
        tk.Button(toolbar, text="Export JSON", command=self.export_json).pack(side="left", padx=3, pady=3)

        self.status_var = tk.StringVar(value="Load an image, then click-drag on it to draw a zone.")
        tk.Label(self, textvariable=self.status_var, anchor="w", fg="gray30").pack(side="top", fill="x", padx=5)

        body = tk.Frame(self)
        body.pack(side="top", fill="both", expand=True)

        self.canvas = tk.Canvas(body, bg="gray20", width=900, height=750)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)

        side = tk.Frame(body, width=320)
        side.pack(side="right", fill="y")
        tk.Label(side, text="Zones (double-click to edit):").pack(anchor="w", padx=5, pady=(5, 0))
        self.zone_listbox = tk.Listbox(side, width=42)
        self.zone_listbox.pack(padx=5, pady=5, fill="y", expand=True)
        self.zone_listbox.bind("<Double-Button-1>", lambda e: self.edit_selected_zone())

    def load_image(self):
        path = filedialog.askopenfilename(filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp")])
        if not path:
            return
        self.image = Image.open(path).convert("RGB")
        self.status_var.set(f"Loaded {path}  ({self.image.size[0]}x{self.image.size[1]})")
        self._render_image()

    def _render_image(self):
        if not self.image:
            return
        max_w, max_h = self.MAX_DISPLAY_SIZE
        w, h = self.image.size
        self.scale = min(max_w / w, max_h / h, 1.0)
        disp_w, disp_h = int(w * self.scale), int(h * self.scale)
        display_img = self.image.resize((disp_w, disp_h), Image.LANCZOS)
        self.tk_image = ImageTk.PhotoImage(display_img)
        self.canvas.config(width=disp_w, height=disp_h)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_image)
        self._redraw_zones()

    def _redraw_zones(self):
        self.canvas.delete("zone")
        if not self.image:
            return
        w, h = self.image.size
        for zone in self.zones:
            box = zone["box"]
            x1 = box["Left"] * w * self.scale
            y1 = box["Top"] * h * self.scale
            x2 = x1 + box["Width"] * w * self.scale
            y2 = y1 + box["Height"] * h * self.scale
            self.canvas.create_rectangle(x1, y1, x2, y2, outline="red", width=2, tags="zone")
            self.canvas.create_text(x1 + 3, y1 + 3, anchor="nw", text=zone["name"], fill="red", font=("Arial", 9, "bold"), tags="zone")

    def on_mouse_down(self, event):
        if not self.image:
            return
        self.rect_start = (event.x, event.y)
        self.current_rect_id = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="yellow", width=2, dash=(4, 2))

    def on_mouse_drag(self, event):
        if not self.rect_start or self.current_rect_id is None:
            return
        x0, y0 = self.rect_start
        self.canvas.coords(self.current_rect_id, x0, y0, event.x, event.y)

    def on_mouse_up(self, event):
        if not self.rect_start or not self.image:
            return
        x0, y0 = self.rect_start
        x1, y1 = event.x, event.y
        self.rect_start = None
        if self.current_rect_id is not None:
            self.canvas.delete(self.current_rect_id)
            self.current_rect_id = None

        left_px, right_px = sorted((x0, x1))
        top_px, bottom_px = sorted((y0, y1))
        if right_px - left_px < 4 or bottom_px - top_px < 4:
            return

        w, h = self.image.size
        box = {
            "Left": round(left_px / self.scale / w, 4),
            "Top": round(top_px / self.scale / h, 4),
            "Width": round((right_px - left_px) / self.scale / w, 4),
            "Height": round((bottom_px - top_px) / self.scale / h, 4),
        }

        dialog = ZoneDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.zones.append(self._ordered_zone(dialog.result, box))
            self._refresh_zone_list()
            self._redraw_zones()

    @staticmethod
    def _ordered_zone(params, box):
        zone = {"name": params["name"], "box": box}
        for key, value in params.items():
            if key != "name":
                zone[key] = value
        return zone

    def _refresh_zone_list(self):
        self.zone_listbox.delete(0, tk.END)
        for zone in self.zones:
            tags = []
            if zone.get("expected_pattern"):
                tags.append("pattern")
            if zone.get("compare_to_reference"):
                tags.append("compare")
            if zone.get("expected_token_count") is not None:
                tags.append(f"count={zone['expected_token_count']}")
            if zone.get("use_textract"):
                tags.append("textract")
            if zone.get("use_vision_llm"):
                tags.append("vision_llm")
            suffix = f"  [{', '.join(tags)}]" if tags else "  [presence-only]"
            self.zone_listbox.insert(tk.END, f"{zone['name']}{suffix}")

    def _selected_zone_index(self):
        sel = self.zone_listbox.curselection()
        return sel[0] if sel else None

    def delete_selected_zone(self):
        idx = self._selected_zone_index()
        if idx is None:
            return
        del self.zones[idx]
        self._refresh_zone_list()
        self._redraw_zones()

    def edit_selected_zone(self):
        idx = self._selected_zone_index()
        if idx is None:
            return
        zone = self.zones[idx]
        dialog = ZoneDialog(self, zone=zone)
        self.wait_window(dialog)
        if dialog.result:
            self.zones[idx] = self._ordered_zone(dialog.result, zone["box"])
            self._refresh_zone_list()
            self._redraw_zones()

    def load_config(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        loaded = data.get("target_regions", [])
        if not loaded:
            messagebox.showwarning("Empty config", "No target_regions found in that file.")
            return
        self.zones = loaded
        self._refresh_zone_list()
        self._redraw_zones()
        self.status_var.set(f"Loaded {len(loaded)} zone(s) from {path}")

    def export_json(self):
        if not self.zones:
            messagebox.showwarning("No zones", "Add at least one zone before exporting.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")], initialfile="spec_config.json")
        if not path:
            return
        data = {"target_regions": self.zones}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        messagebox.showinfo("Exported", f"Saved to:\n{path}")


if __name__ == "__main__":
    app = ZoneEditorApp()
    app.mainloop()
