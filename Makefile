# SPDX-License-Identifier: GPL-2.0

# Avoid surprises by disabling default rules
MAKEFLAGS += --no-builtin-rules
.SUFFIXES:

THIS_EXTERNAL_PATH := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

# Put downloads in this directory instead of in the Buildroot directory
ifeq ($(BR2_DL_DIR),)
BR2_DL_DIR = $(THIS_EXTERNAL_PATH)/dl
endif

OUTPUT_BASEDIR = $(THIS_EXTERNAL_PATH)/output
OUTPUT_DIR = $(OUTPUT_BASEDIR)/$(patsubst %_defconfig,%,$@)

MAKE_BUILDROOT = $(MAKE) -C $(THIS_EXTERNAL_PATH)/buildroot BR2_EXTERNAL=$(THIS_EXTERNAL_PATH)

%: $(THIS_EXTERNAL_PATH)/configs/%
	$(MAKE_BUILDROOT) O=$(OUTPUT_DIR) $@
	sed -i /^BR2_DL_DIR=.*/s%%BR2_DL_DIR=$(BR2_DL_DIR)% $(OUTPUT_DIR)/.config
