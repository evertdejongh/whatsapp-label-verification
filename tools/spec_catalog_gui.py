"""
Local GUI for managing the whatsapp-spec-catalog DynamoDB table (spec_code ->
description, pdf_url, pdf_s3_key) used by the WhatsApp Lambda's spec-sheet
PDF delivery feature.

Requires the local 'evertj-dev' AWS CLI profile -- see
tools/setup_local_dev_user.sh for how that IAM user was created, then run
`aws configure --profile evertj-dev` once to store its keys locally.

Run: python spec_catalog_gui.py
"""
import tkinter as tk
from tkinter import ttk, messagebox
import urllib.request

import boto3

AWS_PROFILE = "evertj-dev"
REGION = "us-east-2"
BUCKET_NAME = "dole-pallet-specs-2026-677513501349-us-east-2-an"
TABLE_NAME = "whatsapp-spec-catalog"

s3 = None
table = None


def init_aws():
    global s3, table
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=REGION)
    s3 = session.client("s3")
    table = session.resource("dynamodb").Table(TABLE_NAME)


def download_pdf(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


class SpecDialog(tk.Toplevel):
    """Popup form for adding/editing one spec catalog entry."""

    def __init__(self, parent, item=None):
        super().__init__(parent)
        self.title("Edit Spec" if item else "Add Spec")
        self.result = None
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        item = item or {}
        self._existing_pdf_url = item.get("pdf_url", "")
        self._existing_pdf_s3_key = item.get("pdf_s3_key")
        row = 0

        tk.Label(self, text="Spec Code:").grid(row=row, column=0, sticky="e", padx=5, pady=3)
        self.spec_code_var = tk.StringVar(value=item.get("spec_code", ""))
        code_entry = tk.Entry(self, textvariable=self.spec_code_var, width=45)
        code_entry.grid(row=row, column=1, sticky="w", padx=5, pady=3)
        if item:
            code_entry.config(state="disabled")  # spec_code is the partition key -- don't allow changing it in place

        row += 1
        tk.Label(self, text="Description:").grid(row=row, column=0, sticky="e", padx=5, pady=3)
        self.description_var = tk.StringVar(value=item.get("description", ""))
        tk.Entry(self, textvariable=self.description_var, width=45).grid(row=row, column=1, sticky="w", padx=5, pady=3)

        row += 1
        tk.Label(self, text="PDF Source URL:").grid(row=row, column=0, sticky="e", padx=5, pady=3)
        self.pdf_url_var = tk.StringVar(value=self._existing_pdf_url)
        tk.Entry(self, textvariable=self.pdf_url_var, width=45).grid(row=row, column=1, sticky="w", padx=5, pady=3)

        row += 1
        hint = "Leave the URL unchanged to keep the currently cached PDF; clear it to remove the PDF."
        tk.Label(self, text=hint, fg="gray40", wraplength=380, justify="left").grid(
            row=row, column=0, columnspan=2, sticky="w", padx=5, pady=(0, 5)
        )

        row += 1
        self.status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.status_var, fg="gray30").grid(row=row, column=0, columnspan=2, sticky="w", padx=5)

        row += 1
        btn_frame = tk.Frame(self)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=10)
        self.save_btn = tk.Button(btn_frame, text="Save", command=self._on_save, width=10)
        self.save_btn.pack(side="left", padx=5)
        tk.Button(btn_frame, text="Cancel", command=self.destroy, width=10).pack(side="left", padx=5)

        self.bind("<Escape>", lambda e: self.destroy())

    def _on_save(self):
        spec_code = self.spec_code_var.get().strip().upper()
        description = self.description_var.get().strip()
        pdf_url = self.pdf_url_var.get().strip()

        if not spec_code:
            messagebox.showerror("Missing spec code", "Spec code is required.", parent=self)
            return

        self.save_btn.config(state="disabled")
        self.config(cursor="watch")
        self.status_var.set("Saving...")
        self.update_idletasks()

        try:
            pdf_s3_key = self._existing_pdf_s3_key
            if pdf_url and pdf_url != self._existing_pdf_url:
                self.status_var.set("Downloading PDF...")
                self.update_idletasks()
                pdf_bytes = download_pdf(pdf_url)

                s3_key = f"specs/{spec_code}_specsheet.pdf"
                self.status_var.set(f"Uploading PDF ({len(pdf_bytes)} bytes)...")
                self.update_idletasks()
                s3.put_object(Bucket=BUCKET_NAME, Key=s3_key, Body=pdf_bytes, ContentType="application/pdf")
                pdf_s3_key = s3_key
            elif not pdf_url:
                pdf_s3_key = None

            item = {"spec_code": spec_code, "description": description}
            if pdf_url:
                item["pdf_url"] = pdf_url
            if pdf_s3_key:
                item["pdf_s3_key"] = pdf_s3_key

            table.put_item(Item=item)
            self.result = item
            self.destroy()
        except Exception as e:
            self.config(cursor="")
            self.save_btn.config(state="normal")
            self.status_var.set("")
            messagebox.showerror("Save failed", str(e), parent=self)


class SpecCatalogApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"WhatsApp Spec Catalog Manager  [{TABLE_NAME}]")
        self.geometry("900x500")

        self._build_ui()
        self.refresh()

    def _build_ui(self):
        toolbar = tk.Frame(self)
        toolbar.pack(side="top", fill="x")
        tk.Button(toolbar, text="Refresh", command=self.refresh).pack(side="left", padx=3, pady=3)
        tk.Button(toolbar, text="Add", command=self.add_spec).pack(side="left", padx=3, pady=3)
        tk.Button(toolbar, text="Edit Selected", command=self.edit_selected).pack(side="left", padx=3, pady=3)
        tk.Button(toolbar, text="Delete Selected", command=self.delete_selected).pack(side="left", padx=3, pady=3)

        self.status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.status_var, anchor="w", fg="gray30").pack(side="top", fill="x", padx=5)

        columns = ("spec_code", "description", "pdf")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("spec_code", text="Spec Code")
        self.tree.heading("description", text="Description")
        self.tree.heading("pdf", text="Cached PDF")
        self.tree.column("spec_code", width=90, anchor="w")
        self.tree.column("description", width=560, anchor="w")
        self.tree.column("pdf", width=180, anchor="w")
        self.tree.pack(side="top", fill="both", expand=True, padx=5, pady=5)
        self.tree.bind("<Double-Button-1>", lambda e: self.edit_selected())

    def refresh(self):
        self.status_var.set("Loading...")
        self.update_idletasks()
        try:
            items = self._scan_all()
        except Exception as e:
            self.status_var.set("")
            messagebox.showerror("Load failed", str(e))
            return

        self.tree.delete(*self.tree.get_children())
        for item in sorted(items, key=lambda i: i["spec_code"]):
            pdf_display = item.get("pdf_s3_key", "(no pdf)")
            self.tree.insert("", "end", iid=item["spec_code"], values=(item["spec_code"], item.get("description", ""), pdf_display))

        self.status_var.set(f"{len(items)} spec(s) loaded.")

    @staticmethod
    def _scan_all():
        resp = table.scan()
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        return items

    def _selected_spec_code(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def add_spec(self):
        dialog = SpecDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.refresh()

    def edit_selected(self):
        spec_code = self._selected_spec_code()
        if not spec_code:
            messagebox.showinfo("No selection", "Select a spec first.")
            return
        try:
            item = table.get_item(Key={"spec_code": spec_code}).get("Item")
        except Exception as e:
            messagebox.showerror("Load failed", str(e))
            return
        if not item:
            messagebox.showerror("Not found", f"No entry for spec_code={spec_code} (may have been deleted elsewhere).")
            self.refresh()
            return

        dialog = SpecDialog(self, item=item)
        self.wait_window(dialog)
        if dialog.result:
            self.refresh()

    def delete_selected(self):
        spec_code = self._selected_spec_code()
        if not spec_code:
            messagebox.showinfo("No selection", "Select a spec first.")
            return

        if not messagebox.askyesno("Confirm delete", f"Delete spec_code={spec_code} from the catalog?"):
            return

        try:
            existing = table.get_item(Key={"spec_code": spec_code}).get("Item") or {}
            table.delete_item(Key={"spec_code": spec_code})

            pdf_s3_key = existing.get("pdf_s3_key")
            if pdf_s3_key and messagebox.askyesno("Delete cached PDF?", f"Also delete the cached PDF from S3?\n{pdf_s3_key}"):
                s3.delete_object(Bucket=BUCKET_NAME, Key=pdf_s3_key)
        except Exception as e:
            messagebox.showerror("Delete failed", str(e))
            return

        self.refresh()


if __name__ == "__main__":
    try:
        init_aws()
        app = SpecCatalogApp()
        app.mainloop()
    except Exception as e:
        # Surface AWS/profile errors (e.g. missing 'evertj-dev' profile) as a
        # message box instead of a bare traceback, since this runs without a
        # console window when double-clicked.
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Startup failed", str(e))
