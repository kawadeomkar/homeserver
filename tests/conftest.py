"""What every test shares: each ansible-playbook a test starts prints with the default stdout callback, whichever
ansible.cfg it reads.

The repo's ansible.cfg names callback_plugins/homeserver.py, which a scratch tree holding a copy of ansible.cfg
alone does not have, and Ansible stops a run whose stdout callback it cannot load ("Could not load 'homeserver'
callback plugin"). No test reads that callback's output by accident either: a test of a callback, or of a recipe
that chooses one, sets ANSIBLE_STDOUT_CALLBACK itself (tests/unit/test_callback.py, test_status.py).
"""

import os

os.environ["ANSIBLE_STDOUT_CALLBACK"] = "default"
