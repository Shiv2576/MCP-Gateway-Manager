<?php
/**
 * Plugin Name: Restock
 * Description: Purchase orders for woo-mcp's restock tools, and a refresh command for the synthetic sales history.
 *
 * Must-use plugin (copied to wp-content/mu-plugins on every store by setup-woo.sh).
 * WooCommerce has no purchase orders; this keeps them as a private post type,
 * listed read-only under WooCommerce > Purchase orders, and serves them over the
 * WooCommerce REST API (wc/v3, so WooCommerce's API keys authenticate it):
 *
 *   GET  /wp-json/wc/v3/restock/orders?status=open|received   (open: ordered or partially_received)
 *   POST /wp-json/wc/v3/restock/orders               {lines: [{product_id, name, qty, unit_cost}], note}
 *   POST /wp-json/wc/v3/restock/orders/<id>/receive  {items: [{product_id, qty}]}
 *
 * woo-mcp checks a plan against the store before it creates an order. Receiving
 * is checked here, under a lock, because it changes stock.
 *
 *   wp restock refresh
 *     re-dates the synthetic history orders (seed.php) so the sales window has data again.
 */

defined( 'ABSPATH' ) || exit;

const RESTOCK_PO = 'restock_po';

add_action(
	'init',
	function () {
		register_post_type(
			RESTOCK_PO,
			array(
				'label'        => 'Purchase orders',
				'public'       => false,
				'show_ui'      => true,
				'show_in_menu' => 'woocommerce',
				'supports'     => array( 'title' ),
				'map_meta_cap' => true,
				'capabilities' => array(
					'create_posts' => 'do_not_allow', // no "Add new": orders come from the API only
					'edit_posts'   => 'manage_woocommerce', // who sees the list
				),
			)
		);
	}
);

// Read-only in WP admin: nobody edits, trashes or deletes a purchase order by hand.
add_filter(
	'map_meta_cap',
	function ( $caps, $cap, $user_id, $args ) {
		if ( in_array( $cap, array( 'edit_post', 'delete_post' ), true ) && ! empty( $args[0] ) && RESTOCK_PO === get_post_type( $args[0] ) ) {
			return array( 'do_not_allow' );
		}
		return $caps;
	},
	10,
	4
);

add_filter(
	'manage_' . RESTOCK_PO . '_posts_columns',
	fn() => array(
		'title'     => 'Order',
		'po_status' => 'Status',
		'po_total'  => 'Total',
		'date'      => 'Ordered',
	)
);
add_action(
	'manage_' . RESTOCK_PO . '_posts_custom_column',
	function ( $column, $post_id ) {
		if ( 'po_status' === $column ) {
			echo esc_html( get_post_meta( $post_id, '_status', true ) );
		} elseif ( 'po_total' === $column ) {
			echo wp_kses_post( wc_price( get_post_meta( $post_id, '_total', true ) ) );
		}
	},
	10,
	2
);

function restock_po_json( WP_Post $post ): array {
	$id = $post->ID;
	return array(
		'id'          => $id,
		'status'      => get_post_meta( $id, '_status', true ),
		'ordered_at'  => get_post_time( 'Y-m-d\TH:i:s', true, $post ),
		'received_at' => get_post_meta( $id, '_received_at', true ) ?: null,
		'total'       => get_post_meta( $id, '_total', true ),
		'note'        => get_post_meta( $id, '_note', true ),
		'lines'       => get_post_meta( $id, '_lines', true ) ?: array(),
	);
}

function restock_create( WP_REST_Request $request ) {
	$lines = array();
	$total = 0.0;
	foreach ( $request['lines'] as $line ) {
		$lines[] = array(
			'product_id' => (int) $line['product_id'],
			'name'       => sanitize_text_field( $line['name'] ),
			'qty'        => (int) $line['qty'],
			'unit_cost'  => $line['unit_cost'],
			'received'   => 0,
		);
		$total += $line['qty'] * (float) $line['unit_cost'];
	}
	$id = wp_insert_post(
		array(
			'post_type'   => RESTOCK_PO,
			'post_status' => 'publish',
			'post_title'  => 'Purchase order',
			'post_author' => get_current_user_id(), // the WooCommerce key's user: who ordered
			'meta_input'  => array(
				'_status' => 'ordered',
				'_lines'  => $lines,
				'_total'  => number_format( $total, 2, '.', '' ),
				'_note'   => sanitize_text_field( $request['note'] ),
			),
		),
		true
	);
	if ( is_wp_error( $id ) ) {
		return $id;
	}
	wp_update_post( array( 'ID' => $id, 'post_title' => "PO #$id" ) );
	return new WP_REST_Response( restock_po_json( get_post( $id ) ), 201 );
}

/** Adds arrived goods to stock. One receive per order at a time, so the check and the write cannot interleave. */
function restock_receive( WP_REST_Request $request ) {
	global $wpdb;
	$id   = (int) $request['id'];
	$lock = "restock_po_$id";
	if ( ! $wpdb->get_var( $wpdb->prepare( 'SELECT GET_LOCK(%s, 5)', $lock ) ) ) {
		return new WP_Error( 'restock_po_busy', "PO #$id is busy, try again", array( 'status' => 409 ) );
	}
	try {
		wp_cache_delete( $id, 'post_meta' ); // read what the previous lock holder wrote
		$post    = get_post( $id );
		$found   = $post && RESTOCK_PO === $post->post_type;
		$status  = $found ? get_post_meta( $id, '_status', true ) : '';
		$lines   = $found ? ( get_post_meta( $id, '_lines', true ) ?: array() ) : array();
		$results = array();
		foreach ( $request['items'] as $item ) {
			$pid  = (int) $item['product_id'];
			$qty  = (int) $item['qty'];
			$fail = fn( $why ) => array( 'product_id' => $pid, 'ok' => false, 'error' => $why );
			$i    = array_search( $pid, array_column( $lines, 'product_id' ), true );
			if ( ! $found ) {
				$results[] = $fail( "PO #$id not found" );
				continue;
			}
			if ( 'received' === $status ) {
				$results[] = $fail( "PO #$id is already fully received" );
				continue;
			}
			if ( false === $i ) {
				$results[] = $fail( "product $pid is not on PO #$id" );
				continue;
			}
			$left = $lines[ $i ]['qty'] - $lines[ $i ]['received'];
			if ( $qty > $left ) {
				$results[] = $fail( "only $left left to receive on PO #$id ({$lines[ $i ]['qty']} ordered, {$lines[ $i ]['received']} received)" );
				continue;
			}
			$product = wc_get_product( $pid );
			if ( ! $product || ! $product->managing_stock() ) {
				$results[] = $fail( $product ? 'stock tracking is off for this product' : 'product not found' );
				continue;
			}
			// shortcut: stock and the order's lines are two writes; a crash between them lets the same units be received again. Move both into one DB transaction if receiving ever runs unattended.
			$stock                    = wc_update_product_stock( $product, $qty, 'increase' );
			$lines[ $i ]['received'] += $qty;
			$results[]                = array( 'product_id' => $pid, 'ok' => true, 'stock' => (int) $stock, 'received' => $lines[ $i ]['received'], 'qty' => $lines[ $i ]['qty'] );
		}
		if ( $found && 'received' !== $status ) {
			update_post_meta( $id, '_lines', $lines );
			if ( ! array_filter( $lines, fn( $l ) => $l['received'] < $l['qty'] ) ) {
				update_post_meta( $id, '_status', 'received' );
				update_post_meta( $id, '_received_at', gmdate( 'Y-m-d\TH:i:s' ) );
			} elseif ( array_filter( $lines, fn( $l ) => $l['received'] > 0 ) ) {
				update_post_meta( $id, '_status', 'partially_received' );
			}
		}
		return array(
			'results' => $results,
			'order'   => $found ? restock_po_json( get_post( $id ) ) : null,
		);
	} finally {
		$wpdb->query( $wpdb->prepare( 'SELECT RELEASE_LOCK(%s)', $lock ) );
	}
}

add_action(
	'rest_api_init',
	function () {
		$can = fn() => current_user_can( 'manage_woocommerce' );
		register_rest_route(
			'wc/v3',
			'/restock/orders',
			array(
				array(
					'methods'             => 'GET',
					'permission_callback' => $can,
					'args'                => array(
						'status' => array(
							'type' => 'string',
							'enum' => array( 'open', 'received' ),
						),
					),
					'callback'            => function ( WP_REST_Request $request ) {
						$query = array(
							'post_type'   => RESTOCK_PO,
							'post_status' => 'publish',
							'numberposts' => -1,
							'orderby'     => 'ID',
							'order'       => 'DESC',
						);
						if ( $request['status'] ) {
							$query['meta_query'] = array(
								array(
									'key'     => '_status',
									'value'   => 'open' === $request['status'] ? array( 'ordered', 'partially_received' ) : array( 'received' ),
									'compare' => 'IN',
								),
							);
						}
						return array_map( 'restock_po_json', get_posts( $query ) );
					},
				),
				array(
					'methods'             => 'POST',
					'permission_callback' => $can,
					'args'                => array(
						'lines' => array(
							'type'     => 'array',
							'required' => true,
							'minItems' => 1,
							'items'    => array(
								'type'       => 'object',
								'properties' => array(
									'product_id' => array( 'type' => 'integer', 'minimum' => 1, 'required' => true ),
									'name'       => array( 'type' => 'string', 'required' => true ),
									'qty'        => array( 'type' => 'integer', 'minimum' => 1, 'required' => true ),
									'unit_cost'  => array( 'type' => 'string', 'pattern' => '^\d+(\.\d{1,2})?$', 'required' => true ),
								),
							),
						),
						'note'  => array(
							'type'    => 'string',
							'default' => '',
						),
					),
					'callback'            => 'restock_create',
				),
			)
		);
		register_rest_route(
			'wc/v3',
			'/restock/orders/(?P<id>\d+)/receive',
			array(
				'methods'             => 'POST',
				'permission_callback' => $can,
				'args'                => array(
					'items' => array(
						'type'     => 'array',
						'required' => true,
						'minItems' => 1,
						'items'    => array(
							'type'       => 'object',
							'properties' => array(
								'product_id' => array( 'type' => 'integer', 'minimum' => 1, 'required' => true ),
								'qty'        => array( 'type' => 'integer', 'minimum' => 1, 'required' => true ),
							),
						),
					),
				),
				'callback'            => 'restock_receive',
			)
		);
	}
);

if ( defined( 'WP_CLI' ) && WP_CLI ) {
	WP_CLI::add_command(
		'restock refresh',
		function () {
			$ids = wc_get_orders(
				array(
					'limit'      => -1,
					'return'     => 'ids',
					'meta_query' => array( array( 'key' => '_synthetic_age', 'compare' => 'EXISTS' ) ),
				)
			);
			foreach ( $ids as $id ) {
				$order = wc_get_order( $id );
				$at    = time() - (int) $order->get_meta( '_synthetic_age' );
				$order->set_date_created( $at );
				if ( $order->get_date_paid() ) {
					$order->set_date_paid( $at );
				}
				if ( $order->get_date_completed() ) {
					$order->set_date_completed( $at );
				}
				$order->save();
			}
			WP_CLI::success( count( $ids ) . ' synthetic orders re-dated' );
		}
	);
}
