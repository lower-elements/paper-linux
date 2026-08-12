ATHEROS_AR6K_TOOLS_VERSION = d6e9690105064eb4171d986aa4c6c5d9e3056727
ATHEROS_AR6K_TOOLS_SOURCE = host.tar.gz
ATHEROS_AR6K_TOOLS_SITE = https://chromium.googlesource.com/chromiumos/third_party/atheros/+archive/$(ATHEROS_AR6K_TOOLS_VERSION)/files
ATHEROS_AR6K_TOOLS_STRIP_COMPONENTS = 0
ATHEROS_AR6K_TOOLS_EXTRA_DOWNLOADS = \
	https://chromium.googlesource.com/chromiumos/third_party/atheros/+archive/$(ATHEROS_AR6K_TOOLS_VERSION)/files/include.tar.gz
ATHEROS_AR6K_TOOLS_LICENSE = GPL-2.0-only
ATHEROS_AR6K_TOOLS_LICENSE_FILES = \
	tools/bmiloader/Makefile \
	tools/eeprom/Makefile \
	tools/wmiconfig/Makefile \
	tools/recEvent/Makefile

define ATHEROS_AR6K_TOOLS_EXTRACT_TARGET_HEADERS
	mkdir -p $(@D)/target-include
	$(TAR) -xf $(ATHEROS_AR6K_TOOLS_DL_DIR)/include.tar.gz \
		-C $(@D)/target-include
endef
ATHEROS_AR6K_TOOLS_POST_EXTRACT_HOOKS += ATHEROS_AR6K_TOOLS_EXTRACT_TARGET_HEADERS

# This SDK predates modern GCC's C inline and prototype semantics. Building as
# GNU89 preserves the dialect expected by the unmodified vendor sources.
ATHEROS_AR6K_TOOLS_CFLAGS = \
	$(TARGET_CFLAGS) \
	-std=gnu89 \
	-Wall \
	-I$(@D)/include \
	-I$(@D)/os/linux/include \
	-I$(@D)/wlan/include \
	-I$(@D)/target-include

define ATHEROS_AR6K_TOOLS_BUILD_CMDS
	$(TARGET_CC) $(ATHEROS_AR6K_TOOLS_CFLAGS) \
		$(@D)/tools/bmiloader/bmiloader.c \
		$(TARGET_LDFLAGS) -o $(@D)/bmiloader
	$(TARGET_CC) $(ATHEROS_AR6K_TOOLS_CFLAGS) \
		$(@D)/tools/eeprom/eeprom.c \
		$(@D)/tools/eeprom/AR6001def.c \
		$(@D)/tools/eeprom/AR6002def.c \
		$(@D)/tools/eeprom/AR6003def.c \
		$(TARGET_LDFLAGS) -o $(@D)/eeprom
	$(TARGET_CC) $(ATHEROS_AR6K_TOOLS_CFLAGS) \
		-DUSER_KEYS $(@D)/tools/wmiconfig/wmiconfig.c \
		$(TARGET_LDFLAGS) -o $(@D)/wmiconfig
	$(TARGET_CC) $(ATHEROS_AR6K_TOOLS_CFLAGS) \
		$(@D)/tools/recEvent/recEvent.c \
		$(TARGET_LDFLAGS) -o $(@D)/recEvent
endef

define ATHEROS_AR6K_TOOLS_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/bmiloader \
		$(TARGET_DIR)/usr/sbin/bmiloader
	$(INSTALL) -D -m 0755 $(@D)/eeprom \
		$(TARGET_DIR)/usr/sbin/eeprom
	$(INSTALL) -D -m 0755 $(@D)/wmiconfig \
		$(TARGET_DIR)/usr/sbin/wmiconfig
	$(INSTALL) -D -m 0755 $(@D)/recEvent \
		$(TARGET_DIR)/usr/sbin/recEvent
endef

$(eval $(generic-package))
