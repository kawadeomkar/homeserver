#!/usr/bin/python

DOCUMENTATION = r"""
---
module: status_write_json
short_description: Write one of just status's files as JSON on the controller
description:
  - Writes O(content) to O(path) as indented JSON, mode 0600, making each missing directory with mode 0700; an
    existing directory is left as it is. The file is written aside and renamed over the old one, so a reader never
    sees half of it.
  - Runs in the controller's own process, as an action plugin (C(action_plugins/status_write_json.py)), so no module
    is sent anywhere. This file holds its documentation, as Ansible's own action plugins keep theirs; run as a
    module, it fails. C(ansible.builtin.copy) of the same content took 0.4 s, two module runs on the controller,
    every time just status ran.
  - In check mode it writes nothing and says whether it would. C(just status) runs as a dry run and takes its exit
    code from the report it writes, so its task turns check mode off (C(check_mode) false).
options:
  path:
    description: The file to write, an absolute path.
    type: str
    required: true
  content:
    description: What to write, as indented JSON with its keys sorted and a newline at the end.
    type: dict
    required: true
attributes:
  check_mode:
    support: full
  diff_mode:
    support: none
  platform:
    platforms: posix
extends_documentation_fragment:
  - ansible.builtin.action_common_attributes
"""

EXAMPLES = r"""
- name: Write the report as JSON
  status_write_json:
    path: "{{ playbook_dir }}/.cache/status/report.json"
    content: "{{ status_report }}"
  check_mode: false
"""

RETURN = r"""
path:
  description: The file written.
  type: str
  returned: always
  sample: /home/me/homeserver/.cache/status/report.json
"""

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(
        argument_spec=dict(
            path=dict(type="str", required=True),
            content=dict(type="dict", required=True),
        ),
        supports_check_mode=True,
    )
    module.fail_json(msg="status_write_json runs on the controller, as an action plugin: it is never sent to a host.")


if __name__ == "__main__":
    main()
