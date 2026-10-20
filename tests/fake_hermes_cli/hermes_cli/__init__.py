"""Doble de hermes_cli construido desde la referencia v0.22.0 (e06df9c) (SUA-07 H2).

Mismas firmas y mismas reglas observables que la referencia copiada en
~/.local/state/encargos-loop/artifacts/SUA07-H2/referencia-hermes-cli/: la
deduplicacion de create_task solo mira tareas NO archivadas, write_txn es
BEGIN IMMEDIATE con anidamiento solo por savepoint, y complete_task/block_task
abren su propia transaccion. Solo lleva lo que las raices y sus pruebas usan.
"""
