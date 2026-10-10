#!/bin/bash
# Sets up WooCommerce on the store. Runs as a Helm hook after every install and
# upgrade, so every step is safe to repeat.
set -euo pipefail

# Same layout the WordPress pod builds: config and wp-content live on the PVC.
cp -r /opt/bitnami/wordpress/. /app/
ln -sfn /bitnami/wordpress/wp-config.php /app/wp-config.php
rm -rf /app/wp-content && ln -s /bitnami/wordpress/wp-content /app/wp-content
wp() { /opt/bitnami/wp-cli/bin/wp --path=/app "$@"; }

# The Service only routes to a Ready pod, i.e. after Bitnami's first-boot install.
until curl -so /dev/null "http://$WP_SERVICE/"; do echo "waiting for WordPress"; sleep 5; done
wp core is-installed

wp rewrite structure '/%postname%/'

wp plugin is-installed woocommerce || wp plugin install woocommerce
wp plugin activate woocommerce
wp wc tool run install_pages --user=1
# Home page = the shop's product list.
wp option update show_on_front page
wp option update page_on_front "$(wp option get woocommerce_shop_page_id)"
# New WooCommerce stores start in "coming soon" mode and hide the shop.
wp option update woocommerce_coming_soon no
wp option update woocommerce_currency "$CURRENCY"
wp option update woocommerce_default_country "$COUNTRY"
wp option update woocommerce_cod_settings --format=json \
  '{"enabled":"yes","title":"Cash on delivery","description":"Pay with cash upon delivery.","instructions":"Pay with cash upon delivery.","enable_for_methods":[],"enable_for_virtual":"yes"}'

if [ "$(wp wc product list --user=1 --format=count)" = 0 ]; then
  wp eval-file /setup/seed.php
fi

wp eval-file /setup/register-woo-key.php
echo "store setup done"
