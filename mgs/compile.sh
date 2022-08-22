#!/bin/bash

TOOLS_PATH=$PSYQ_SDK

PSYQ=$PSYQ_SDK/psyq_4.4
export PSYQ_PATH="$PSYQ/bin"
export C_PLUS_INCLUDE_PATH="$PSYQ/include"
export C_INCLUDE_PATH="$PSYQ/include"
export LIBRARY_PATH="$PSYQ/lib"
export PSX_PATH="$PSYQ/bin"

export WSLENV="PSYQ_PATH/p:C_PLUS_INCLUDE_PATH/p:C_INCLUDE_PATH/p:LIBRARY_PATH/p:PSX_PATH/p"

$TOOLS_PATH/psyq_4.4/bin/CCPSX.EXE -O2 -G 8 -g0 -c "\\\\wsl$\\Ubuntu$1" -o "\\\\wsl$\\Ubuntu$3"

