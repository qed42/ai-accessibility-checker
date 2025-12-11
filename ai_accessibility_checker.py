import os
import re
import json
import html
import argparse
from pathlib import Path
from datetime import datetime
from openai import OpenAI
from dotenv import load_dotenv
from tabulate import tabulate
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.lib.enums import TA_LEFT, TA_CENTER

# -------------------------
# Load API Key from .env or ENV
# -------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    load_dotenv()
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    print("❌ OpenAI API key not found in .env file or environment variable.")
    print("Please create a .env file with:\n\n  OPENAI_API_KEY=your_key_here")
    exit(1)

client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------
# Load Config (checker.config.json)
# -------------------------
def load_config():
    config_file = Path("checker.config.json")
    if not config_file.exists():
        print("⚠️ No checker.config.json found. Using default settings.")
        return {
            "SUPPORTED_EXTENSIONS": [".html", ".twig", ".css", ".scss", ".pcss", ".jsx", ".tsx"],
            "EXCLUDED_DIRS": ["node_modules", "storybook", ".git", "__pycache__", "dist", "build"],
            "EXCLUDED_PATTERNS": [".stories.jsx", ".stories.tsx"],
            "MODEL": "gpt-4o"
        }
    with open(config_file, "r", encoding="utf-8") as f:
        return json.load(f)

CONFIG = load_config()

# -------------------------
# User Inputs (interactive or CLI)
# -------------------------
def get_user_inputs():
    parser = argparse.ArgumentParser(description="AI Accessibility Checker")
    parser.add_argument("--level", choices=["A", "AA", "AAA"], help="WCAG accessibility level")
    parser.add_argument("--version", choices=["2.0", "2.1", "2.2"], help="WCAG version")
    parser.add_argument("--format", choices=["table", "list", "pdf"], default="table", help="Output format")
    parser.add_argument("--dir", default=os.getcwd(), help="Directory to scan")

    args = parser.parse_args()

    # If CLI args provided, use them (CI mode)
    if args.level and args.version:
        return args.level, args.version, args.format, args.dir

    # Otherwise, ask interactively (local mode)
    print("\n👋 Welcome to AI Accessibility Checker\n")
    
    level = input("🧩 Which WCAG accessibility level? (A / AA / AAA): ").upper().strip()
    while level not in ["A", "AA", "AAA"]:
        level = input("❗ Please enter a valid level (A / AA / AAA): ").upper().strip()

    version = input("📘 Which WCAG version? (2.0 / 2.1 / 2.2): ").strip()
    while version not in ["2.0", "2.1", "2.2"]:
        version = input("❗ Please enter a valid version (2.0 / 2.1 / 2.2): ").strip()

    output_format = input("📊 How would you like results? (table / list / pdf): ").strip().lower()
    if output_format not in ["table", "list", "pdf"]:
        print("⚠️ Invalid choice. Defaulting to 'table'.")
        output_format = "table"

    path = input("📂 Enter the directory path to scan (leave blank for current directory): ").strip()
    if not path:
        path = os.getcwd()

    return level, version, output_format, path

# -------------------------
# File Finder
# -------------------------
def find_supported_files(directory):
    files_to_scan = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in CONFIG["EXCLUDED_DIRS"]]
        for file in files:
            if (
                any(file.endswith(ext) for ext in CONFIG["SUPPORTED_EXTENSIONS"])
                and not any(pat in file for pat in CONFIG["EXCLUDED_PATTERNS"])
            ):
                files_to_scan.append(os.path.join(root, file))
    return files_to_scan

# -------------------------
# AI Analysis
# -------------------------
def scan_with_ai(content, file_name, level, version):
    # Detect file type for template-aware analysis
    file_ext = Path(file_name).suffix.lower()
    
    # Template syntax guidance based on file type
    template_guidance = ""
    if file_ext in [".twig", ".html"]:
        template_guidance = """
**IMPORTANT - Template Syntax Awareness:**
This file contains Twig template syntax. Distinguish between these two patterns:

1. **Variables that render COMPLETE HTML elements** (DO NOT FLAG):
   - Examples: {{ image }}, {{ content.field_image }}, {{ node.field_banner }}, {{ item.image }}
   - These variables output entire HTML tags (e.g., complete <img> with all attributes)
   - The backend/CMS handles accessibility attributes automatically
   - DO NOT flag these as missing alt text - they are handled server-side

2. **Manual HTML tags with dynamic ATTRIBUTES** (FLAG if missing alt):
   - Examples: <img src="{{ image_url }}">, <img src="{{ path }}">
   - The template is building the HTML tag structure itself
   - If the template lacks alt attribute entirely (not even alt="{{ var }}"), FLAG it
   - If template has alt="{{ variable }}" or alt="{{ item.alt }}", DO NOT flag

**Key Rule**: Only flag accessibility issues when the TEMPLATE STRUCTURE itself is building an incomplete HTML element. Do NOT flag when a variable is outputting a complete, pre-rendered HTML element.
"""
    elif file_ext in [".jsx", ".tsx"]:
        template_guidance = """
**IMPORTANT - JSX/React Awareness:**
This file contains JSX/React code. Distinguish between these patterns:

1. **Components/Functions that render COMPLETE accessible elements** (DO NOT FLAG):
   - Examples: <Image />, <NextImage />, <GatsbyImage />, {renderImage()}, {content.image}
   - Framework image components (Next.js Image, Gatsby Image, custom Image components)
   - Functions that return complete JSX with accessibility built-in
   - DO NOT flag these - they handle accessibility internally

2. **Manual <img> tags with dynamic PROPS** (FLAG if missing alt):
   - Examples: <img src={imageUrl} />, <img src={props.image} />
   - The component is building the HTML tag structure itself
   - If the JSX lacks alt prop entirely (not even alt={variable}), FLAG it
   - If JSX has alt={variable}, alt={props.alt}, or alt={alt || ''}, DO NOT flag
   - Exception: alt="" is acceptable for decorative images

3. **Image components WITH explicit props** (Verify alt exists):
   - Examples: <Image src={url} alt={description} />
   - Even custom components should have alt prop for accessibility
   - Flag if custom image component lacks alt prop entirely

**Key Rule**: Only flag when the component/element structure itself lacks accessibility props. Recognize that framework components and render functions handle accessibility internally.
"""
    
    prompt = f"""
You are an expert in web accessibility and WCAG compliance.

The following code includes line numbers.
{template_guidance}

Scan the code and return **only valid JSON** with this structure:
[
  {{
    "title": "Short title of the issue",
    "issue_type": "Type/category of the issue (e.g., Contrast, Alt Text, Keyboard Navigation)",
    "description": "Detailed description of the issue",
    "line_numbers": [list of affected lines],
    "code_snippet": "Relevant code snippet",
    "suggestion": "AI-based suggestion to fix it",
    "severity": "High | Medium | Low"
  }}
]

Rules:
- Do not include any extra text outside JSON.
- Severity should be based on WCAG impact.
- If no issues found, return [].
- For template files: Recognize that variables/expressions provide dynamic content at runtime.
- Only flag issues when the template structure itself is inaccessible, not when dynamic content might fix it.

WCAG Version: {version}
Accessibility Level: {level}

File: {file_name}
----------------------
{content}
"""

    try:
        response = client.chat.completions.create(
            model=CONFIG["MODEL"],
            messages=[
                {"role": "system", "content": "You are an expert accessibility auditor."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3
        )

        raw_output = response.choices[0].message.content.strip()
        raw_output = re.sub(r"^```(json)?|```$", "", raw_output, flags=re.MULTILINE).strip()

        match = re.search(r"\[.*\]", raw_output, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        else:
            return []

    except json.JSONDecodeError as e:
        print(f"⚠️ JSON parsing error: {e}")
        return []
    except Exception as e:
        print(f"❌ Error scanning file {file_name}: {str(e)}")
        return []

# -------------------------
# PDF Export
# -------------------------
def export_to_pdf(all_results, level, version, directory):
    """
    Generate a PDF report from accessibility scan results.
    all_results: list of tuples (file_path, issues_list)
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_filename = f"accessibility_report_{timestamp}.pdf"
    
    doc = SimpleDocTemplate(pdf_filename, pagesize=letter,
                           rightMargin=0.5*inch, leftMargin=0.5*inch,
                           topMargin=0.5*inch, bottomMargin=0.5*inch)
    
    story = []
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor=colors.HexColor('#1a1a1a'),
        spaceAfter=12,
        alignment=TA_CENTER
    )
    
    heading_style = ParagraphStyle(
        'CustomHeading',
        parent=styles['Heading2'],
        fontSize=14,
        textColor=colors.HexColor('#2c5aa0'),
        spaceAfter=10,
        spaceBefore=10
    )
    
    file_heading_style = ParagraphStyle(
        'FileHeading',
        parent=styles['Heading2'],
        fontSize=12,
        textColor=colors.HexColor('#d9534f'),
        spaceAfter=8,
        spaceBefore=12
    )
    
    # Title
    story.append(Paragraph("Accessibility Analysis Report", title_style))
    story.append(Spacer(1, 0.2*inch))
    
    # Metadata
    metadata = [
        ["WCAG Version:", version],
        ["Accessibility Level:", level],
        ["Scan Directory:", directory],
        ["Report Generated:", datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
    ]
    
    meta_table = Table(metadata, colWidths=[2*inch, 4*inch])
    meta_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f0f0f0')),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey)
    ]))
    
    story.append(meta_table)
    story.append(Spacer(1, 0.3*inch))
    
    # Summary
    total_files = len(all_results)
    total_issues = sum(len(issues) for _, issues in all_results)
    files_with_issues = sum(1 for _, issues in all_results if issues)
    
    story.append(Paragraph(f"Summary: {total_issues} issue(s) found across {files_with_issues} of {total_files} file(s)", heading_style))
    story.append(Spacer(1, 0.2*inch))
    
    # Issues by file
    for file_path, issues in all_results:
        story.append(Paragraph(f"File: {os.path.basename(file_path)}", file_heading_style))
        story.append(Paragraph(f"<font size=8 color='#666666'>{file_path}</font>", styles['Normal']))
        story.append(Spacer(1, 0.1*inch))
        
        if not issues:
            # Display "No issues found" message
            no_issue_style = ParagraphStyle(
                'NoIssue',
                parent=styles['Normal'],
                fontSize=10,
                textColor=colors.HexColor('#28a745'),
                spaceAfter=8
            )
            story.append(Paragraph("✅ No accessibility issues found.", no_issue_style))
            story.append(Spacer(1, 0.2*inch))
            continue
        
        # Create table for issues
        table_data = [["#", "Title", "Type", "Severity", "Lines", "Description", "Suggestion"]]
        
        for idx, issue in enumerate(issues, 1):
            severity = issue.get('severity', 'N/A')
            
            # Color code severity
            if severity == 'High':
                severity_color = '<font color="red"><b>High</b></font>'
            elif severity == 'Medium':
                severity_color = '<font color="orange"><b>Medium</b></font>'
            else:
                severity_color = '<font color="green">Low</font>'
            
            table_data.append([
                str(idx),
                Paragraph(html.escape(issue.get('title', 'N/A')), styles['Normal']),
                Paragraph(html.escape(issue.get('issue_type', 'N/A')), styles['Normal']),
                Paragraph(severity_color, styles['Normal']),
                Paragraph(', '.join(map(str, issue.get('line_numbers', []))), styles['Normal']),
                Paragraph(html.escape(issue.get('description', 'N/A')), styles['Normal']),
                Paragraph(html.escape(issue.get('suggestion', 'N/A')), styles['Normal'])
            ])
        
        issue_table = Table(table_data, colWidths=[0.3*inch, 1.1*inch, 0.85*inch, 0.7*inch, 0.8*inch, 1.7*inch, 1.7*inch])
        issue_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c5aa0')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('FONTSIZE', (0, 1), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f9f9f9')])
        ]))
        
        story.append(issue_table)
        story.append(Spacer(1, 0.3*inch))
    
    # Build PDF
    doc.build(story)
    return pdf_filename

# -------------------------
# Main Runner
# -------------------------
def main():
    # --- Compliance / Acknowledgement ---
    if os.getenv("AI_CHECKER_ACKNOWLEDGED") != "true":
        print("⚠️ This tool sends your code snippets to the OpenAI API for processing.")
        print("Please ensure your project has no contractual or compliance restrictions before continuing.")
        resp = input("Do you acknowledge and wish to continue? (yes/no): ").strip().lower()
        if resp != "yes":
            print("Exiting. You must acknowledge before running.")
            exit(0)

    level, version, output_format, directory = get_user_inputs()
    files_to_scan = find_supported_files(directory)

    if not files_to_scan:
        print("⚠️ No supported files found in the specified directory.")
        return

    print(f"\n🔍 Scanning {len(files_to_scan)} file(s) for WCAG {version} ({level}) issues...\n")

    # Store all results for PDF generation
    all_results = []

    for file in files_to_scan:
        try:
            with open(file, 'r', encoding='utf-8') as f:
                content = f.read()

            print(f"📄 Scanning: {file}")
            numbered_content = "\n".join(f"{i+1:4}: {line}" for i, line in enumerate(content.splitlines()))
            issues = scan_with_ai(numbered_content, os.path.basename(file), level, version)

            # Store results for PDF
            all_results.append((file, issues))

            if not issues:
                print("✅ No accessibility issues found.\n")
                continue

            if output_format == "table":
                table_data = [
                    [
                        i+1,
                        issue.get("title", ""),
                        issue.get("issue_type", ""),
                        issue.get("severity", ""),
                        ", ".join(map(str, issue.get("line_numbers", []))),
                        issue.get("description", ""),
                        issue.get("suggestion", "")
                    ]
                    for i, issue in enumerate(issues)
                ]
                headers = ["#", "Issue Title", "Issue Type", "Severity", "Line(s)", "Description", "Suggestion"]
                print(tabulate(table_data, headers=headers, tablefmt="grid", maxcolwidths=[None, 25, 15, 10, 10, 40, 40]))
                print("\n" + "-"*100 + "\n")

            elif output_format == "list":
                for idx, issue in enumerate(issues, start=1):
                    print(f"\n{idx}. {issue.get('title', '')} [{issue.get('issue_type', '')}] (Severity: {issue.get('severity', '')})")
                    print(f"   Lines: {', '.join(map(str, issue.get('line_numbers', [])))}")
                    print(f"   Description: {issue.get('description', '')}")
                    print(f"   Suggestion: {issue.get('suggestion', '')}")
                    print("-"*80)

        except Exception as e:
            print(f"❗ Could not read {file}: {str(e)}")
    
    # Generate PDF if requested
    if output_format == "pdf":
        pdf_file = export_to_pdf(all_results, level, version, directory)
        print(f"\n✅ PDF report generated: {pdf_file}")

if __name__ == "__main__":
    main()
