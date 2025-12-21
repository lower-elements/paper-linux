FBINK_VERSION = v1.25.0
FBINK_SOURCE = $(FBINK_VERSION).tar.gz
FBINK_SITE = https://github.com/NiLuJe/FBInk/archive/refs/tags
FBINK_LICENSE = GPL-3.0-or-later
FBINK_LICENSE_FILES = LICENSE
FBINK_INSTALL_STAGING = YES
FBINK_FONT8X8_VERSION = fa74831b9076805271cdedc0ee12137aa548e524
FBINK_EXTRA_DOWNLOADS = https://github.com/NiLuJe/font8x8/archive/$(FBINK_FONT8X8_VERSION).tar.gz

FBINK_MAKE_OPTS = \
	CROSS_TC= \
	CROSS_COMPILE= \
	KINDLE=1 \
	MINIMAL=1 \
	BITMAP=1 \
	CC="$(TARGET_CC)" \
	CXX="$(TARGET_CXX)" \
	AR="$(TARGET_AR)" \
	RANLIB="$(TARGET_RANLIB)" \
	STRIP="$(TARGET_STRIP)" \
	CFLAGS="$(TARGET_CFLAGS)" \
	LDFLAGS="$(TARGET_LDFLAGS)"

define FBINK_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) $(FBINK_MAKE_OPTS) staticlib
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) $(FBINK_MAKE_OPTS) sharedlib
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) $(FBINK_MAKE_OPTS) sharedbin
endef

define FBINK_EXTRACT_FONT8X8
	tar -xf $(FBINK_DL_DIR)/$(FBINK_FONT8X8_VERSION).tar.gz -C $(@D)
	rm -rf $(@D)/font8x8
	mv $(@D)/font8x8-$(FBINK_FONT8X8_VERSION) $(@D)/font8x8
endef
FBINK_POST_EXTRACT_HOOKS += FBINK_EXTRACT_FONT8X8

define FBINK_INSTALL_STAGING_CMDS
	$(INSTALL) -D -m 644 $(@D)/fbink.h $(STAGING_DIR)/usr/include/fbink/fbink.h
	mkdir -p $(STAGING_DIR)/usr/lib
	cp -a $(@D)/Release/libfbink.so* $(STAGING_DIR)/usr/lib/
endef

define FBINK_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 755 $(@D)/Release/fbink $(TARGET_DIR)/usr/bin/fbink
	mkdir -p $(TARGET_DIR)/usr/lib
	cp -a $(@D)/Release/libfbink.so* $(TARGET_DIR)/usr/lib/
endef

$(eval $(generic-package))
