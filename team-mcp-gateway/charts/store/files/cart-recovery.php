<?php
/**
 * Plugin Name: Cart recovery
 * Description: Abandoned carts for woo-mcp. Records when a logged-in customer's cart last changed and lists those carts over the WooCommerce REST API.
 *
 * Must-use plugin (copied to wp-content/mu-plugins by setup-woo.sh). WooCommerce
 * already saves a logged-in customer's cart in user meta, but its REST API does
 * not expose carts and does not record when they changed. This adds both:
 *
 *   GET /wp-json/wc/v3/cart-recovery/carts?days=20
 *     [{user_id, updated_at, items: [{product_id, quantity}]}]
 *
 * Under wc/v3, so WooCommerce's API keys authenticate it like any other call.
 * No names or emails: the caller only needs counts.
 *
 *   wp cart-recovery refresh
 *     re-dates the synthetic shoppers' carts (seed.php) so the demo window has data again.
 */

defined( 'ABSPATH' ) || exit;

const CART_RECOVERY_UPDATED = '_cart_updated_at';
const CART_RECOVERY_SPREAD  = 30 * DAY_IN_SECONDS;

/** How long ago synthetic shopper $n last touched their cart: fixed per shopper, so every seed and refresh agree. */
function cart_recovery_synthetic_age( int $n ): int {
	return crc32( "cart-recovery-shopper-$n" ) % CART_RECOVERY_SPREAD;
}

// The same events on which WooCommerce writes the saved cart.
foreach ( array( 'woocommerce_add_to_cart', 'woocommerce_cart_item_removed', 'woocommerce_cart_item_restored', 'woocommerce_cart_item_set_quantity' ) as $hook ) {
	add_action(
		$hook,
		function () {
			if ( get_current_user_id() ) {
				update_user_meta( get_current_user_id(), CART_RECOVERY_UPDATED, time() );
			}
		}
	);
}

/*
 * A WooCommerce session cookie that no longer verifies (left over from a store that
 * was deleted and re-created at the same address) makes WooCommerce pick a new random
 * guest id on every request, yet still sign form nonces with it: the login form's
 * nonce never verifies and the login silently fails. Sign with the plain logged-out
 * id instead, and expire the stale cookie so the browser heals.
 */
add_filter(
	'nonce_user_logged_out',
	function ( $uid ) {
		$name = apply_filters( 'woocommerce_cookie', 'wp_woocommerce_session_' . COOKIEHASH );
		if ( empty( $_COOKIE[ $name ] ) || ! function_exists( 'WC' ) || ! WC()->session || false !== WC()->session->get_session_cookie() ) {
			return $uid;
		}
		if ( ! headers_sent() ) {
			setcookie( $name, '', time() - YEAR_IN_SECONDS, COOKIEPATH ?: '/', COOKIE_DOMAIN );
		}
		return 0;
	},
	20 // after WooCommerce's own filter
);

add_action(
	'rest_api_init',
	function () {
		register_rest_route(
			'wc/v3',
			'/cart-recovery/carts',
			array(
				'methods'             => 'GET',
				'permission_callback' => fn() => current_user_can( 'manage_woocommerce' ),
				'args'                => array(
					'days' => array(
						'type'    => 'integer',
						'minimum' => 1,
						'maximum' => 90,
						'default' => 20,
					),
				),
				'callback'            => function ( WP_REST_Request $request ) {
					$users = get_users(
						array(
							'fields'     => 'ID',
							'meta_query' => array(
								array(
									'key'     => CART_RECOVERY_UPDATED,
									'value'   => time() - $request['days'] * DAY_IN_SECONDS,
									'compare' => '>=',
									'type'    => 'NUMERIC',
								),
							),
						)
					);
					$key   = '_woocommerce_persistent_cart_' . get_current_blog_id();
					$carts = array();
					foreach ( $users as $user_id ) {
						$saved = get_user_meta( $user_id, $key, true );
						$items = array();
						foreach ( (array) ( $saved['cart'] ?? array() ) as $line ) {
							if ( ! empty( $line['product_id'] ) ) {
								$items[] = array(
									'product_id' => (int) $line['product_id'],
									'quantity'   => (int) $line['quantity'],
								);
							}
						}
						if ( $items ) { // an order empties the cart, so bought carts drop out here
							$carts[] = array(
								'user_id'    => (int) $user_id,
								'updated_at' => (int) get_user_meta( $user_id, CART_RECOVERY_UPDATED, true ),
								'items'      => $items,
							);
						}
					}
					return $carts;
				},
			)
		);
	}
);

if ( defined( 'WP_CLI' ) && WP_CLI ) {
	WP_CLI::add_command(
		'cart-recovery refresh',
		function () {
			$users = get_users(
				array(
					'meta_key'   => '_synthetic_shopper',
					'meta_value' => '1',
				)
			);
			foreach ( $users as $user ) {
				$n = (int) substr( $user->user_login, strlen( 'shopper' ) );
				update_user_meta( $user->ID, CART_RECOVERY_UPDATED, time() - cart_recovery_synthetic_age( $n ) );
			}
			WP_CLI::success( count( $users ) . ' synthetic carts re-dated over the last 30 days' );
		}
	);
}
