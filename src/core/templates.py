"""templates — land-record field templates: load / save / apply.

Reads the JSON starting-field sets under config/templates/. Applying a template
to a parcel copies its fields as editable {label, value} rows; editing a parcel
never touches the template. Not implemented yet (Milestone 6). Pure Python.
"""
