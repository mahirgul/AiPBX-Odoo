#!/usr/bin/env python3
"""Make the module for another Odoo version from the 17.0 code (one source; each store branch
is this output): build/<v>.0/aipbx_connector*.  Usage: make_version.py 18|19"""
import pathlib
import re
import shutil
import sys

v = int(sys.argv[1])
here = pathlib.Path(__file__).resolve().parent


def convert(dst):
    manifest = dst / "__manifest__.py"
    manifest.write_text(manifest.read_text().replace('"version": "17.0.', f'"version": "{v}.0.'))

    if v >= 18:
        # 18 renamed the tree view to list.
        for xml in dst.rglob("*.xml"):
            t = xml.read_text()
            t = re.sub(r"<tree\b", "<list", t).replace("</tree>", "</list>")
            t = t.replace(">tree,form<", ">list,form<")
            xml.write_text(t)
    if v >= 19:
        # 19: groups belong to a privilege (with the category), users are user_ids.
        sec = dst / "security" / "aipbx_security.xml"
        if sec.exists():
            t = sec.read_text()
            t = t.replace('<field name="category_id" ref="module_category_aipbx"/>',
                          '<field name="privilege_id" ref="privilege_aipbx"/>')
            privilege = ('    <record id="privilege_aipbx" model="res.groups.privilege">\n'
                         '        <field name="name">AiPBX</field>\n'
                         '        <field name="category_id" ref="module_category_aipbx"/>\n'
                         '    </record>\n')
            t = t.replace('    <record id="group_aipbx_user"', privilege + '    <record id="group_aipbx_user"', 1)
            t = t.replace('<field name="users" eval=', '<field name="user_ids" eval=')
            sec.write_text(t)
        # 19: _sql_constraints became models.Constraint attributes.
        for py in (dst / "models").glob("*.py"):
            t = py.read_text()
            t = re.sub(r'_sql_constraints = \[\("(\w+)", ("[^"]+"), ("[^"]+")\)\]',
                       lambda m: f"_{m.group(1)} = models.Constraint({m.group(2)}, {m.group(3)})", t)
            py.write_text(t)


out = here / "build" / f"{v}.0"
shutil.rmtree(out, ignore_errors=True)
for name in ("aipbx_connector", "aipbx_connector_crm"):
    shutil.copytree(here / name, out / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
for dst in sorted(out.iterdir()):
    convert(dst)
print(out)

