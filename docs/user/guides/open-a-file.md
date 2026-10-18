---
title: Open a Nova Design or InDesign (IDML) file
description: Open a .novadesign document or an InDesign Markup (.idml) file, and what to do about missing fonts and incomplete IDML packages.
order: 20
verified: 0.7.1
---

Choose **File**, then **Open…** (Ctrl+O), and pick a `.novadesign` or `.idml` file. The file opens as a new document.

Shortcuts on this page use Ctrl. On macOS, use Cmd instead of Ctrl.

## Steps

1. Open the **File** menu and choose **Open…**. On the start screen you can click **Open** instead.
2. In the file dialog, pick a file. The dialog lists **Nova Design or IDML** (both, listed first), **Nova Design** (`.novadesign`) and **InDesign Markup (IDML)** (`.idml`).
3. Confirm the dialog. The document opens as a new document.

## What the Open dialog accepts

The Open dialog lists `.novadesign` and `.idml` files only. To bring an image, a PDF or a text file into a document, use [Place](place-images-and-text.md) instead.

IDML is InDesign Markup, the exchange format InDesign can export. Linked images are read from next to the file or from its `Links` folder. If a `Document Fonts` folder sits beside the file, Nova Design loads those fonts first.

## Missing fonts

Text that uses a font Nova Design cannot find is highlighted in pink. This is the missing-font highlight. Put the font files in a `Document Fonts` folder beside the file, or install the fonts on your system, so Nova Design can find them.

## Import warnings

If an IDML package is missing some of its parts, Nova Design still opens it and shows an **Import Warnings** alert that names each missing part. The contents of those parts are not in the document, so check your layout.

If the package has none of its spreads, there is nothing to open. Nova Design then shows a **Can't Open the File** alert that names the missing parts.

## Save it again

**File**, then **Save** (Ctrl+S) writes a `.novadesign` file. To hand the layout back to InDesign, use **File**, then **Export IDML…**.
