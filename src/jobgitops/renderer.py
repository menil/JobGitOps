"""Resume compilation and rendering pipeline using Jinja2 and WeasyPrint."""

import json
import pathlib

from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

from jobgitops.schema import Resume


def render_resume_to_html(resume: Resume, template_path: str | pathlib.Path) -> str:
    """Render a parsed Resume object to HTML using a Jinja2 template.

    Args:
        resume: The parsed Resume instance to render.
        template_path: Path to the Jinja2 HTML template file.

    Returns:
        The rendered HTML string.

    Raises:
        FileNotFoundError: If the template file does not exist.
    """
    template_path = pathlib.Path(template_path)
    if not template_path.is_file():
        raise FileNotFoundError(f"Template file not found at: {template_path}")

    # Set up FileSystemLoader pointing to the template's directory.
    # This allows resolving template inheritance or includes if the template grows.
    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        autoescape=True,
    )
    template = env.get_template(template_path.name)

    # Pass individual fields to match standard JSON Resume schema conventions
    # used in the default HTML templates (e.g., {{ basics.name }}).
    return template.render(
        basics=resume.basics,
        work=resume.work,
        education=resume.education,
        skills=resume.skills,
        projects=resume.projects,
    )


def compile_resume_pdf(
    resume: Resume,
    template_path: str | pathlib.Path,
    output_pdf_path: str | pathlib.Path,
) -> None:
    """Compile a Resume object to a PDF file using WeasyPrint.

    Args:
        resume: The parsed Resume instance to compile.
        template_path: Path to the Jinja2 HTML template file.
        output_pdf_path: Output target path for the compiled PDF file.
    """
    template_path = pathlib.Path(template_path)
    output_pdf_path = pathlib.Path(output_pdf_path)

    # Ensure target output directory exists before writing
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    rendered_html = render_resume_to_html(resume, template_path)

    # We set base_url to the template directory so WeasyPrint can resolve
    # the linked style.css (and any images) relative to the template.
    html_doc = HTML(string=rendered_html, base_url=str(template_path.parent))
    html_doc.write_pdf(output_pdf_path)


def compile_resume_json(
    resume: Resume,
    output_json_path: str | pathlib.Path,
) -> None:
    """Serialize a Resume object to a standard JSON Resume file.

    Args:
        resume: The parsed Resume instance to serialize.
        output_json_path: Output target path for the JSON file.
    """
    output_json_path = pathlib.Path(output_json_path)

    # Ensure target output directory exists before writing
    output_json_path.parent.mkdir(parents=True, exist_ok=True)

    serialized_data = resume.to_dict()

    with output_json_path.open("w", encoding="utf-8") as f:
        # indent=2 and ensure_ascii=False keeps the generated JSON resume clean,
        # human-readable, and properly formatted with UTF-8 characters.
        json.dump(serialized_data, f, indent=2, ensure_ascii=False)


def compile_resume(
    resume: Resume,
    template_path: str | pathlib.Path,
    output_pdf_path: str | pathlib.Path,
    output_json_path: str | pathlib.Path,
) -> None:
    """Compile both the PDF and JSON representations of the resume.

    Args:
        resume: The parsed Resume instance.
        template_path: Path to the Jinja2 HTML template file.
        output_pdf_path: Output target path for the compiled PDF.
        output_json_path: Output target path for the JSON resume.
    """
    compile_resume_pdf(resume, template_path, output_pdf_path)
    compile_resume_json(resume, output_json_path)
