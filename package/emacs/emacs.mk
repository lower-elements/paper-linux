################################################################################
#
# emacs
#
################################################################################

EMACS_VERSION = 30.2
EMACS_SOURCE = emacs-$(EMACS_VERSION).tar.xz
EMACS_SITE = https://ftp.gnu.org/gnu/emacs
EMACS_LICENSE = GPL-3.0-or-later
EMACS_LICENSE_FILES = COPYING

EMACS_DEPENDENCIES = ncurses host-emacs host-pkgconf $(TARGET_NLS_DEPENDENCIES)
EMACS_CONF_ENV = \
	ac_cv_prog_NCURSESW_CONFIG=$(STAGING_DIR)/usr/bin/$(NCURSES_CONFIG_SCRIPTS)
EMACS_MAKE_ENV = MAKEINFO=true
EMACS_BUILD_OPTS = \
	BUILD_EMACS="$(HOST_DIR)/bin/emacs" \
	MAKE_DOCFILE="$(HOST_DIR)/bin/emacs-make-docfile" \
	MAKE_FINGERPRINT="$(HOST_DIR)/bin/emacs-make-fingerprint" \
	bootstrap_exe="$(HOST_DIR)/bin/emacs"
EMACS_INSTALL_OPTS = \
	BUILD_EMACS="$(HOST_DIR)/bin/emacs" \
	MAKE_DOCFILE="$(HOST_DIR)/bin/emacs-make-docfile" \
	MAKE_FINGERPRINT="$(HOST_DIR)/bin/emacs-make-fingerprint"

EMACS_CONF_OPTS = \
	--disable-acl \
	--disable-xattr \
	--with-x=no \
	--with-x-toolkit=no \
	--without-xpm \
	--without-jpeg \
	--without-tiff \
	--without-gif \
	--without-png \
	--without-rsvg \
	--without-webp \
	--without-lcms2 \
	--without-libsystemd \
	--without-cairo \
	--without-xft \
	--without-harfbuzz \
	--without-libotf \
	--without-m17n-flt \
	--without-toolkit-scroll-bars \
	--without-xaw3d \
	--without-xim \
	--without-xdbe \
	--without-gpm \
	--without-dbus \
	--without-gsettings \
	--without-selinux \
	--without-modules \
	--with-mailutils=no \
	--with-pop=no \
	--with-kerberos=no \
	--with-kerberos5=no \
	--with-hesiod=no \
	--with-sound=no \
	--with-file-notification=no \
	--with-dumping=none

ifeq ($(BR2_REPRODUCIBLE),y)
EMACS_CONF_OPTS += --disable-build-details
endif

ifeq ($(BR2_PACKAGE_EMACS_ZLIB),y)
EMACS_DEPENDENCIES += zlib
EMACS_CONF_OPTS += --with-zlib
else
EMACS_CONF_OPTS += --without-zlib
endif

ifeq ($(BR2_PACKAGE_EMACS_GNUTLS),y)
EMACS_DEPENDENCIES += gnutls
EMACS_CONF_OPTS += --with-gnutls
else
EMACS_CONF_OPTS += --without-gnutls
endif

ifeq ($(BR2_PACKAGE_EMACS_XML2),y)
EMACS_DEPENDENCIES += libxml2
EMACS_CONF_OPTS += --with-xml2
else
EMACS_CONF_OPTS += --without-xml2
endif

ifeq ($(BR2_PACKAGE_EMACS_SQLITE3),y)
EMACS_DEPENDENCIES += sqlite
EMACS_CONF_OPTS += --with-sqlite3
else
EMACS_CONF_OPTS += --without-sqlite3
endif

define EMACS_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(EMACS_MAKE_ENV) $(MAKE) -C $(@D) $(EMACS_BUILD_OPTS) \
		actual-all
endef

define EMACS_INSTALL_TARGET_CMDS
	$(TARGET_MAKE_ENV) $(EMACS_MAKE_ENV) $(MAKE) -C $(@D) $(EMACS_INSTALL_OPTS) \
		DESTDIR=$(TARGET_DIR) install

	rm -rf $(TARGET_DIR)/usr/share/applications
	rm -rf $(TARGET_DIR)/usr/share/icons
	rm -rf $(TARGET_DIR)/usr/share/man
	rm -rf $(TARGET_DIR)/usr/share/info
endef

HOST_EMACS_DEPENDENCIES = host-gmp host-pkgconf host-ncurses

HOST_EMACS_CONF_OPTS = \
	--prefix=$(HOST_DIR) \
	--without-all \
	--with-file-notification=no \
	--with-hesiod=no \
	--with-kerberos=no \
	--with-kerberos5=no \
	--with-mailutils=no \
	--with-pop=no \
	--with-sound=no \
	--with-x=no \
	--with-x-toolkit=no \
	--disable-acl \
	--without-dbus \
	--without-gpm \
	--without-gnutls \
	--without-gsettings \
	--without-modules \
	--without-selinux \
	--without-sqlite3 \
	--without-xml2 \
	--without-zlib

ifeq ($(BR2_REPRODUCIBLE),y)
HOST_EMACS_CONF_OPTS += --disable-build-details
endif

define HOST_EMACS_CONFIGURE_CMDS
	mkdir -p $(@D)/build
	cd $(@D)/build && \
		PATH="$(HOST_DIR)/bin:$$PATH" \
		MAKEINFO=true \
		ac_cv_prog_NCURSESW_CONFIG="$(HOST_DIR)/bin/ncursesw6-config" \
		$(HOST_CONFIGURE_OPTS) \
		../configure \
			$(HOST_EMACS_CONF_OPTS)
endef

define HOST_EMACS_BUILD_CMDS
	$(HOST_MAKE_ENV) $(MAKE) -C $(@D)/build MAKEINFO=true all
endef

define HOST_EMACS_INSTALL_CMDS
	$(HOST_MAKE_ENV) $(MAKE) -C $(@D)/build MAKEINFO=true install
	$(INSTALL) -D -m 755 $(@D)/build/lib-src/make-docfile \
		$(HOST_DIR)/bin/emacs-make-docfile
	$(INSTALL) -D -m 755 $(@D)/build/lib-src/make-fingerprint \
		$(HOST_DIR)/bin/emacs-make-fingerprint

	rm -rf $(HOST_DIR)/share/applications
	rm -rf $(HOST_DIR)/share/icons
	rm -rf $(HOST_DIR)/share/info
	rm -rf $(HOST_DIR)/share/man
endef

$(eval $(autotools-package))
$(eval $(host-generic-package))
