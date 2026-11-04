#!/bin/python3

# What the worker tells the server about itself: its GPUs (gpus).
# It may never fail a request. Run from UnitTests/ with the Client's requirements installed.

import os
import sys

PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir))
sys.path.insert(0, os.path.join(PARENT, 'Client'))


def test_gpus_are_names_and_any_failure_is_none(monkeypatch):
    import worker

    lspci = [
        '00:02.0 VGA compatible controller: Intel Corporation UHD Graphics',
        '00:14.0 USB controller: Intel Corporation USB 3.0',
        '01:00.0 3D controller: NVIDIA Corporation GA104 [GeForce RTX 3070]',
    ]
    monkeypatch.setattr(worker.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(worker.platform, 'release', lambda: '6.1.0-generic')
    monkeypatch.setattr(worker, 'command_lines', lambda command: lspci)
    assert worker.detect_gpus() == ['Intel Corporation UHD Graphics', 'NVIDIA Corporation GA104 [GeForce RTX 3070]']

    # Under WSL, nvidia-smi is asked first
    monkeypatch.setattr(worker.platform, 'release', lambda: '5.15.0-microsoft-standard-WSL2')
    smi = ['GPU 0: NVIDIA GeForce RTX 4070 (UUID: GPU-1234)']
    monkeypatch.setattr(worker, 'command_lines', lambda command: smi if command[0] == 'nvidia-smi' else lspci)
    assert worker.detect_gpus() == ['NVIDIA GeForce RTX 4070']

    # Windows: the names as printed
    monkeypatch.setattr(worker.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(worker, 'command_lines', lambda command: ['NVIDIA GeForce RTX 4070'])
    assert worker.detect_gpus() == ['NVIDIA GeForce RTX 4070']

    # A missing tool, a timeout or anything else is an empty list
    monkeypatch.undo()
    def boom(*args, **kw): raise FileNotFoundError()
    monkeypatch.setattr(worker.subprocess, 'run', boom)
    assert worker.command_lines(['lspci']) == []
    assert worker.detect_gpus() == []
