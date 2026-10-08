import os
import json
import re
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory
import pikepdf
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas
from werkzeug.utils import secure_filename

from gulagcleaner.clean import clean_pdf_path
from gulagcleaner.decrypt import decrypt_pdf


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None


@app.get("/")
def index():
    return send_from_directory(app.root_path, "index.html")


def _save_pdf_upload(uploaded_file, directory, filename):
    safe_name = secure_filename(uploaded_file.filename or "")
    if not safe_name or Path(safe_name).suffix.lower() != ".pdf":
        raise ValueError("Selecciona un archivo PDF válido.")

    path = Path(directory) / filename
    uploaded_file.save(path)
    with path.open("rb") as pdf_file:
        if pdf_file.read(5) != b"%PDF-":
            raise ValueError("El archivo no parece ser un PDF válido.")
    return path, safe_name


def _open_pdf(path, password=""):
    return pikepdf.Pdf.open(path, password=password or "")


def _parse_pages(value, page_count):
    pages = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if re.fullmatch(r"\d+\s*-\s*\d+", part):
            first, last = [int(number.strip()) for number in part.split("-", 1)]
            if first > last:
                raise ValueError("Los rangos deben ir en orden ascendente.")
            pages.extend(range(first, last + 1))
        elif part.isdigit():
            pages.append(int(part))
        else:
            raise ValueError("Usa páginas como 1,3-5.")

    if not pages:
        raise ValueError("Indica al menos una página.")
    if any(page < 1 or page > page_count for page in pages):
        raise ValueError("El rango contiene páginas fuera del documento.")
    if len(pages) != len(set(pages)):
        raise ValueError("No repitas páginas o rangos.")
    return pages


def _send_pdf_response(temporary_directory, path, filename, mimetype="application/pdf"):
    response = send_file(
        path,
        mimetype=mimetype,
        as_attachment=True,
        download_name=filename,
    )
    response.direct_passthrough = False
    response.call_on_close(temporary_directory.cleanup)
    return response


def _get_single_upload(temporary_directory):
    uploaded_file = request.files.get("archivo_pdf")
    if uploaded_file is None or not uploaded_file.filename:
        raise ValueError("Selecciona un archivo PDF.")
    return _save_pdf_upload(uploaded_file, temporary_directory, "source.pdf")


@app.post("/pdf/unir")
def unir_pdfs():
    uploaded_files = request.files.getlist("archivos")
    if len(uploaded_files) < 2:
        return jsonify(error="Selecciona al menos dos archivos PDF."), 400

    temporary_directory = tempfile.TemporaryDirectory(prefix="cavivi-merge-")
    output_path = Path(temporary_directory.name) / "merged.pdf"
    output_pdf = pikepdf.Pdf.new()
    try:
        password = request.form.get("password", "")
        source_pdfs = []
        source_names = []
        for index, uploaded_file in enumerate(uploaded_files):
            if not uploaded_file.filename:
                raise ValueError("Uno de los archivos no tiene nombre.")
            source_path, _ = _save_pdf_upload(uploaded_file, temporary_directory.name, "source-{}.pdf".format(index))
            source_pdf = _open_pdf(source_path, password)
            source_pdfs.append(source_pdf)
            source_names.append(uploaded_file.filename)

        expected_pages = {
            (file_index, page_number)
            for file_index, source_pdf in enumerate(source_pdfs)
            for page_number in range(1, len(source_pdf.pages) + 1)
        }
        order_payload = request.form.get("page_order", "")
        if order_payload:
            try:
                requested_order = json.loads(order_payload)
            except json.JSONDecodeError:
                raise ValueError("El orden de páginas recibido no es válido.")
            if not isinstance(requested_order, list):
                raise ValueError("El orden de páginas recibido no es válido.")
            page_order = []
            for item in requested_order:
                if not isinstance(item, dict) or type(item.get("fileIndex")) is not int or type(item.get("pageNumber")) is not int:
                    raise ValueError("El orden de páginas recibido no es válido.")
                page_order.append((item["fileIndex"], item["pageNumber"]))
            if len(page_order) != len(expected_pages) or set(page_order) != expected_pages:
                raise ValueError("El orden debe incluir cada página de cada PDF una sola vez.")
        else:
            page_order = [(file_index, page_number) for file_index, source_pdf in enumerate(source_pdfs) for page_number in range(1, len(source_pdf.pages) + 1)]

        for file_index, page_number in page_order:
            output_pdf.pages.extend([source_pdfs[file_index].pages[page_number - 1]])
        if not len(output_pdf.pages):
            raise ValueError("Los PDF seleccionados no contienen páginas.")
        output_pdf.save(output_path)
        output_pdf.close()
        for source_pdf in source_pdfs:
            source_pdf.close()
        return _send_pdf_response(temporary_directory, output_path, "pdfs_unidos.pdf")
    except ValueError as error:
        output_pdf.close()
        for source_pdf in locals().get("source_pdfs", []):
            source_pdf.close()
        temporary_directory.cleanup()
        return jsonify(error=str(error)), 400
    except pikepdf.PasswordError:
        output_pdf.close()
        for source_pdf in locals().get("source_pdfs", []):
            source_pdf.close()
        temporary_directory.cleanup()
        return jsonify(error="La contraseña de uno de los PDF no es válida."), 400
    except Exception:
        output_pdf.close()
        for source_pdf in locals().get("source_pdfs", []):
            source_pdf.close()
        temporary_directory.cleanup()
        app.logger.exception("Error al unir PDF")
        return jsonify(error="No se pudieron unir los PDF."), 500


@app.post("/pdf/reordenar")
def reordenar_pdf():
    temporary_directory = tempfile.TemporaryDirectory(prefix="cavivi-reorder-")
    output_path = Path(temporary_directory.name) / "reordered.pdf"
    output_pdf = pikepdf.Pdf.new()
    try:
        source_path, source_name = _get_single_upload(temporary_directory.name)
        with _open_pdf(source_path, request.form.get("password", "")) as source_pdf:
            order = [int(part.strip()) for part in request.form.get("orden", "").split(",") if part.strip()]
            expected = list(range(1, len(source_pdf.pages) + 1))
            if sorted(order) != expected:
                raise ValueError("Escribe todas las páginas una sola vez, por ejemplo 3,1,2.")
            output_pdf.pages.extend(source_pdf.pages[number - 1] for number in order)
            output_pdf.save(output_path)
        output_pdf.close()
        return _send_pdf_response(temporary_directory, output_path, "reordenado_" + source_name)
    except ValueError as error:
        output_pdf.close()
        temporary_directory.cleanup()
        return jsonify(error=str(error)), 400
    except pikepdf.PasswordError:
        output_pdf.close()
        temporary_directory.cleanup()
        return jsonify(error="La contraseña del PDF no es válida."), 400
    except Exception:
        output_pdf.close()
        temporary_directory.cleanup()
        app.logger.exception("Error al reordenar PDF")
        return jsonify(error="No se pudo reordenar el PDF."), 500


@app.post("/pdf/eliminar")
def eliminar_paginas_pdf():
    temporary_directory = tempfile.TemporaryDirectory(prefix="cavivi-remove-pages-")
    output_path = Path(temporary_directory.name) / "pages-removed.pdf"
    output_pdf = pikepdf.Pdf.new()
    try:
        source_path, source_name = _get_single_upload(temporary_directory.name)
        with _open_pdf(source_path, request.form.get("password", "")) as source_pdf:
            removed_pages = set(_parse_pages(request.form.get("pages", ""), len(source_pdf.pages)))
            kept_pages = [page_number for page_number in range(1, len(source_pdf.pages) + 1) if page_number not in removed_pages]
            if not kept_pages:
                raise ValueError("No puedes eliminar todas las páginas del PDF.")
            output_pdf.pages.extend(source_pdf.pages[page_number - 1] for page_number in kept_pages)
            output_pdf.save(output_path)
        output_pdf.close()
        return _send_pdf_response(temporary_directory, output_path, "paginas_eliminadas_" + source_name)
    except ValueError as error:
        output_pdf.close()
        temporary_directory.cleanup()
        return jsonify(error=str(error)), 400
    except pikepdf.PasswordError:
        output_pdf.close()
        temporary_directory.cleanup()
        return jsonify(error="La contraseña del PDF no es válida."), 400
    except Exception:
        output_pdf.close()
        temporary_directory.cleanup()
        app.logger.exception("Error al eliminar páginas PDF")
        return jsonify(error="No se pudieron eliminar las páginas del PDF."), 500


@app.post("/pdf/numerar")
def numerar_pdf():
    temporary_directory = tempfile.TemporaryDirectory(prefix="cavivi-number-")
    output_path = Path(temporary_directory.name) / "numbered.pdf"
    overlays = []
    try:
        source_path, source_name = _get_single_upload(temporary_directory.name)
        position = request.form.get("position", "bottom-right")
        positions = {
            "top-left", "top-center", "top-right",
            "middle-left", "center", "middle-right",
            "bottom-left", "bottom-center", "bottom-right",
        }
        if position not in positions:
            raise ValueError("Elige una esquina válida para la numeración.")
        try:
            first_number = int(request.form.get("start", "1"))
        except ValueError:
            raise ValueError("El número inicial debe ser un entero.")
        if first_number < 0:
            raise ValueError("El número inicial no puede ser negativo.")
        page_format = request.form.get("format", "{n}")
        if "{n}" not in page_format or len(page_format) > 80:
            raise ValueError("El formato debe incluir {n} y tener como máximo 80 caracteres.")
        font = request.form.get("font", "Helvetica")
        supported_fonts = {"Helvetica", "Helvetica-Bold", "Times-Roman", "Times-Bold", "Courier", "Courier-Bold"}
        if font not in supported_fonts:
            raise ValueError("La tipografía seleccionada no está disponible.")
        try:
            font_size = int(request.form.get("size", "12"))
            text_color = HexColor(request.form.get("color", "#333333"))
        except (ValueError, TypeError):
            raise ValueError("Revisa el tamaño y el color de la numeración.")
        if not 6 <= font_size <= 48:
            raise ValueError("El tamaño debe estar entre 6 y 48 puntos.")

        with _open_pdf(source_path, request.form.get("password", "")) as source_pdf:
            total_pages = len(source_pdf.pages)
            for page_index, page in enumerate(source_pdf.pages):
                x0, y0, x1, y1 = [float(value) for value in page.mediabox]
                width, height = x1 - x0, y1 - y0
                margin = max(18, min(width, height) * 0.035)
                label = page_format.replace("{n}", str(first_number + page_index)).replace("{total}", str(total_pages))
                overlay_stream = BytesIO()
                overlay_canvas = canvas.Canvas(overlay_stream, pagesize=(width, height))
                overlay_canvas.setFont(font, font_size)
                overlay_canvas.setFillColor(text_color)
                horizontal = position.split("-")[-1]
                vertical = position.split("-")[0]
                overlay_x = x0 + margin if horizontal == "left" else x1 - margin if horizontal == "right" else x0 + width / 2
                overlay_y = y0 + margin if vertical == "bottom" else y1 - margin - font_size if vertical == "top" else y0 + (height - font_size) / 2
                if horizontal == "right":
                    overlay_canvas.drawRightString(overlay_x, overlay_y, label)
                elif horizontal == "center":
                    overlay_canvas.drawCentredString(overlay_x, overlay_y, label)
                else:
                    overlay_canvas.drawString(overlay_x, overlay_y, label)
                overlay_canvas.save()
                overlay_pdf = pikepdf.Pdf.open(BytesIO(overlay_stream.getvalue()))
                page.add_overlay(overlay_pdf.pages[0], rect=pikepdf.Rectangle(x0, y0, x1, y1))
                overlays.append(overlay_pdf)
            source_pdf.save(output_path)
        for overlay_pdf in overlays:
            overlay_pdf.close()
        return _send_pdf_response(temporary_directory, output_path, "numerado_" + source_name)
    except ValueError as error:
        for overlay_pdf in overlays:
            overlay_pdf.close()
        temporary_directory.cleanup()
        return jsonify(error=str(error)), 400
    except pikepdf.PasswordError:
        for overlay_pdf in overlays:
            overlay_pdf.close()
        temporary_directory.cleanup()
        return jsonify(error="La contraseña del PDF no es válida."), 400
    except Exception:
        for overlay_pdf in overlays:
            overlay_pdf.close()
        temporary_directory.cleanup()
        app.logger.exception("Error al numerar PDF")
        return jsonify(error="No se pudo numerar el PDF."), 500


@app.post("/pdf/dividir")
def dividir_pdf():
    temporary_directory = tempfile.TemporaryDirectory(prefix="cavivi-split-")
    archive_path = Path(temporary_directory.name) / "paginas.zip"
    try:
        source_path, source_name = _get_single_upload(temporary_directory.name)
        with _open_pdf(source_path, request.form.get("password", "")) as source_pdf:
            split_mode = request.form.get("mode", "range")
            if split_mode == "all":
                selected_pages = list(range(1, len(source_pdf.pages) + 1))
            else:
                selected_pages = _parse_pages(request.form.get("pages", ""), len(source_pdf.pages))

            if split_mode == "all":
                with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                    for page_number in selected_pages:
                        single_page_pdf = pikepdf.Pdf.new()
                        single_page_pdf.pages.extend([source_pdf.pages[page_number - 1]])
                        page_stream = BytesIO()
                        single_page_pdf.save(page_stream)
                        single_page_pdf.close()
                        archive.writestr("pagina_{}.pdf".format(page_number), page_stream.getvalue())
                return _send_pdf_response(temporary_directory, archive_path, "paginas_individuales.zip", "application/zip")

            extracted_pdf = pikepdf.Pdf.new()
            extracted_pdf.pages.extend(source_pdf.pages[page_number - 1] for page_number in selected_pages)
            extracted_path = Path(temporary_directory.name) / "extracted.pdf"
            extracted_pdf.save(extracted_path)
            extracted_pdf.close()
        return _send_pdf_response(temporary_directory, extracted_path, "paginas_" + source_name)
    except ValueError as error:
        temporary_directory.cleanup()
        return jsonify(error=str(error)), 400
    except pikepdf.PasswordError:
        temporary_directory.cleanup()
        return jsonify(error="La contraseña del PDF no es válida."), 400
    except Exception:
        temporary_directory.cleanup()
        app.logger.exception("Error al dividir PDF")
        return jsonify(error="No se pudo dividir el PDF."), 500


@app.post("/pdf/rotar")
def rotar_pdf():
    temporary_directory = tempfile.TemporaryDirectory(prefix="cavivi-rotate-")
    output_path = Path(temporary_directory.name) / "rotated.pdf"
    try:
        source_path, source_name = _get_single_upload(temporary_directory.name)
        try:
            angle = int(request.form.get("angle", "90"))
        except ValueError:
            raise ValueError("El giro debe ser 90, 180 o 270 grados.")
        if angle not in (90, 180, 270):
            raise ValueError("El giro debe ser 90, 180 o 270 grados.")

        with _open_pdf(source_path, request.form.get("password", "")) as source_pdf:
            raw_pages = request.form.get("pages", "all").strip().lower()
            selected_pages = list(range(1, len(source_pdf.pages) + 1)) if raw_pages == "all" else _parse_pages(raw_pages, len(source_pdf.pages))
            for page_number in selected_pages:
                source_pdf.pages[page_number - 1].rotate(angle, relative=True)
            source_pdf.save(output_path)
        return _send_pdf_response(temporary_directory, output_path, "rotado_" + source_name)
    except ValueError as error:
        temporary_directory.cleanup()
        return jsonify(error=str(error)), 400
    except pikepdf.PasswordError:
        temporary_directory.cleanup()
        return jsonify(error="La contraseña del PDF no es válida."), 400
    except Exception:
        temporary_directory.cleanup()
        app.logger.exception("Error al rotar PDF")
        return jsonify(error="No se pudo rotar el PDF."), 500


@app.post("/pdf/comprimir")
def comprimir_pdf():
    temporary_directory = tempfile.TemporaryDirectory(prefix="cavivi-compress-")
    output_path = Path(temporary_directory.name) / "compressed.pdf"
    try:
        source_path, source_name = _get_single_upload(temporary_directory.name)
        with _open_pdf(source_path, request.form.get("password", "")) as source_pdf:
            source_pdf.save(
                output_path,
                compress_streams=True,
                recompress_flate=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                linearize=True,
            )
        return _send_pdf_response(temporary_directory, output_path, "comprimido_" + source_name)
    except ValueError as error:
        temporary_directory.cleanup()
        return jsonify(error=str(error)), 400
    except pikepdf.PasswordError:
        temporary_directory.cleanup()
        return jsonify(error="La contraseña del PDF no es válida."), 400
    except Exception:
        temporary_directory.cleanup()
        app.logger.exception("Error al comprimir PDF")
        return jsonify(error="No se pudo optimizar el PDF."), 500


@app.post("/pdf/proteger")
def proteger_pdf():
    temporary_directory = tempfile.TemporaryDirectory(prefix="cavivi-protect-")
    output_path = Path(temporary_directory.name) / "protected.pdf"
    try:
        source_path, source_name = _get_single_upload(temporary_directory.name)
        user_password = request.form.get("new_password", "")
        if len(user_password) < 4:
            raise ValueError("La contraseña debe tener al menos 4 caracteres.")
        owner_password = request.form.get("owner_password", "") or user_password
        with _open_pdf(source_path, request.form.get("password", "")) as source_pdf:
            source_pdf.save(
                output_path,
                encryption=pikepdf.Encryption(owner=owner_password, user=user_password, R=6),
            )
        return _send_pdf_response(temporary_directory, output_path, "protegido_" + source_name)
    except ValueError as error:
        temporary_directory.cleanup()
        return jsonify(error=str(error)), 400
    except pikepdf.PasswordError:
        temporary_directory.cleanup()
        return jsonify(error="La contraseña del PDF de origen no es válida."), 400
    except Exception:
        temporary_directory.cleanup()
        app.logger.exception("Error al proteger PDF")
        return jsonify(error="No se pudo proteger el PDF."), 500


@app.post("/pdf/desproteger")
def desproteger_pdf():
    temporary_directory = tempfile.TemporaryDirectory(prefix="cavivi-unprotect-")
    output_path = Path(temporary_directory.name) / "unprotected.pdf"
    try:
        source_path, source_name = _get_single_upload(temporary_directory.name)
        password = request.form.get("password", "")
        if not password:
            raise ValueError("Escribe la contraseña actual del PDF.")
        with _open_pdf(source_path, password) as source_pdf:
            source_pdf.save(output_path, encryption=False)
        return _send_pdf_response(temporary_directory, output_path, "desprotegido_" + source_name)
    except ValueError as error:
        temporary_directory.cleanup()
        return jsonify(error=str(error)), 400
    except pikepdf.PasswordError:
        temporary_directory.cleanup()
        return jsonify(error="La contraseña no es válida o no se pudo abrir el PDF."), 400
    except Exception:
        temporary_directory.cleanup()
        app.logger.exception("Error al desproteger PDF")
        return jsonify(error="No se pudo desproteger el PDF."), 500


@app.post("/limpiar")
def limpiar_pdf():
    if "archivo_pdf" not in request.files:
        return jsonify(error="No se subió ningún archivo."), 400

    uploaded_file = request.files["archivo_pdf"]
    if not uploaded_file.filename:
        return jsonify(error="El archivo no tiene nombre."), 400

    safe_name = secure_filename(uploaded_file.filename)
    if not safe_name or Path(safe_name).suffix.lower() != ".pdf":
        return jsonify(error="Selecciona un archivo PDF válido."), 400

    temporary_directory = tempfile.TemporaryDirectory(prefix="cavivi-pdf-")
    temporary_root = Path(temporary_directory.name).resolve()
    source_path = temporary_root / "uploaded.pdf"
    output_path = temporary_root / "cleaned.pdf"

    try:
        uploaded_file.save(source_path)
        with source_path.open("rb") as source_file:
            if source_file.read(5) != b"%PDF-":
                temporary_directory.cleanup()
                return jsonify(error="El archivo no parece ser un PDF válido."), 400

        decrypted_path = decrypt_pdf(str(source_path))
        if not decrypted_path:
            raise RuntimeError("No se pudo preparar el PDF para limpiarlo.")

        result = clean_pdf_path(str(decrypted_path), str(output_path), force_naive=False)
        if not isinstance(result, dict) or not result.get("success"):
            temporary_directory.cleanup()
            return jsonify(error="GulagCleaner no pudo limpiar este PDF."), 422

        returned_path = result.get("return_path")
        cleaned_path = Path(returned_path).resolve() if returned_path else output_path
        if os.path.commonpath([str(temporary_root), str(cleaned_path)]) != str(temporary_root):
            raise RuntimeError("El limpiador devolvió una ruta de salida no válida.")
        if not cleaned_path.is_file():
            raise RuntimeError("El PDF limpio no se generó correctamente.")

        response = send_file(
            cleaned_path,
            mimetype="application/pdf",
            as_attachment=True,
            download_name="clean_" + safe_name,
        )
        response.direct_passthrough = False
        response.call_on_close(temporary_directory.cleanup)
        return response
    except Exception:
        temporary_directory.cleanup()
        app.logger.exception("Error al limpiar el PDF subido")
        return jsonify(error="Se produjo un error al limpiar el PDF."), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)