**`collect_files.py`** tool is designed to work with **any text-based file type**. It does not hard-code an extension list — instead, it checks if the file looks like **text** or **binary**.

That means it will **include all text-like files** (no matter the extension) and **skip binary-like files** (images, videos, executables, etc.).

---

### ✅ Common supported file types

Here are examples of file types it will successfully read and include in the output:

* **Programming source files**

  * Python: `.py`, `.pyw`, `.pyi`
  * Dart / Flutter: `.dart`, `.yaml`, `.log`
  * Web: `.html`, `.css`, `.js`, `.ts`, `.jsx`, `.tsx`
  * Java / C-family: `.java`, `.c`, `.cpp`, `.h`, `.cs`
  * Scripts: `.sh`, `.bat`, `.ps1`, `.pl`, `.rb`, `.php`

* **Configuration & metadata**

  * `.json`, `.toml`, `.yaml`, `.yml`, `.ini`, `.cfg`, `.conf`, `.env`

* **Data files (text-based)**

  * `.sql`, `.csv`, `.tsv`, `.txt`, `.ndjson`

* **Documentation**

  * `.md`, `.rst`, `.adoc`, `.tex`

* **Project-specific files**

  * `.lock` files (`package-lock.json`, `pubspec.lock`)
  * Logs: `.log`
  * Makefiles, Dockerfiles, `requirements.txt`, etc.

Basically, **any file that stores plain text**.

---

### ❌ Files that are normally skipped

The tool automatically skips files that are detected as **binary** (to avoid corrupt output), such as:

* Images (`.png`, `.jpg`, `.gif`, `.ico`, `.svgz`)
* Audio/video (`.mp3`, `.mp4`, `.wav`, `.mov`)
* Compiled / executables (`.exe`, `.dll`, `.so`, `.o`, `.class`, `.jar`)
* Archives (`.zip`, `.tar`, `.gz`, `.7z`, `.rar`)
* Large binary blobs (detected by null bytes, high binary ratio, or exceeding `--max-size`)

---

👉 In short:

* If the file is **plain text**, it’s supported (no matter the extension).
* If the file is **binary-like**, it’s skipped (safe by design).

---
s