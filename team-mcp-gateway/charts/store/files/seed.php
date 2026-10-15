<?php
/**
 * Seeds an empty store in one WordPress boot (one wp-cli call per product
 * takes 1-2 s each; hundreds of them would not fit the provisioning timeout).
 *
 *   TEMPLATE=catalog-a|catalog-b|blank wp eval-file seed.php
 *
 * catalog-a: the restock planning demo. 40 everyday products with a cost of
 * goods and tracked stock, and 90 days of sales (about 1,500 guest orders)
 * made from each product's units-per-day rate, so the restock tools see a
 * stockout hero (Fresh Deodorant 150ml, 6 days left), a thin-margin fast mover,
 * a sold-out product, overstock and dead stock. History orders carry
 * _synthetic_age for `wp restock refresh` (mu-plugin restock.php). Same fixed
 * seed, same history in every store.
 * catalog-b: the cart recovery demo. 40 products with a cost of goods
 * (WooCommerce's Cost of Goods Sold field) and 300 shoppers (shopper001@example.com ...) with saved carts last
 * changed 0-30 days ago; 80 carts hold Fresh Deodorant 150ml. Same fixed seed,
 * same carts in every store. Shopper password: SHOPPER_PASSWORD, else random.
 * blank: the products in /setup/products ("name|price" lines).
 * Then a few orders created now (catalog-a: none, its history reaches today), so "today's orders" has data.
 * The caller runs this only when the store has no products.
 */

$catalogs = array(
	// Restock planning demo: name, price, cost of goods (null: unknown), stock, units sold per day,
	// and, for a sold-out product, how many days ago its sales stopped.
	'catalog-a' => array(
		'seed'           => 101,
		'orders'         => 0, // the history reaches today
		'history_days'   => 90,
		'stock_products' => array(
			'Personal Care' => array(
				array( 'Fresh Deodorant 150ml', 249, 110, 54, 9 ), array( 'Herbal Shampoo 400ml', 399, 170, 30, 2 ),
				array( 'Mint Toothpaste 150g', 129, 62, 270, 6 ), array( 'Neem Soap 4-pack', 199, 120, 180, 4 ),
				array( 'Aloe Face Wash 100ml', 249, 95, 135, 3 ), array( 'Coconut Hair Oil 300ml', 189, 105, 150, 3 ),
				array( 'Sunscreen SPF 50', 499, 210, 45, 1 ), array( 'Shaving Foam 200ml', 220, 120, 70, 1.5 ),
			),
			'Snacks'        => array(
				array( 'Instant Noodles 12-pack', 168, 150, 40, 8 ), array( 'Salted Peanuts 200g', 99, 58, 25, 3 ),
				array( 'Masala Chips 52g', 20, 11, 400, 5 ), array( 'Roasted Makhana 100g', 149, 80, 90, 2 ),
				array( 'Digestive Biscuits 250g', 60, 42, 270, 6 ), array( 'Dark Chocolate 70%', 249, 130, 50, 1 ),
				array( 'Trail Mix 200g', 299, 170, 45, 1 ), array( 'Banana Chips 200g', 110, 60, 90, 2 ),
			),
			'Beverages'     => array(
				array( 'Green Tea 100 Bags', 349, 120, 0, 4, 3 ), array( 'Filter Coffee 500g', 450, 260, 120, 2.5 ),
				array( 'Masala Chai 250g', 220, 110, 135, 3 ), array( 'Mango Juice 1L', 120, 78, 225, 5 ),
				array( 'Lemon Soda 6-pack', 180, 110, 135, 3 ), array( 'Coconut Water 1L', 150, 95, 90, 2 ),
				array( 'Hot Chocolate Mix 400g', 399, 230, 40, 0.75 ), array( 'Kombucha 330ml', 199, 120, 25, 0.5 ),
			),
			'Household'     => array(
				array( 'Detergent Powder 1kg', 135, 118, 200, 5 ), array( 'Dishwash Gel 500ml', 110, 62, 180, 4 ),
				array( 'Floor Cleaner 1L', 199, 105, 80, 2 ), array( 'Garbage Bags x30', 99, 45, 135, 3 ),
				array( 'Toilet Rolls 10-pack', 399, 250, 70, 1.5 ), array( 'Kitchen Towels 2-roll', 149, 80, 90, 2 ),
				array( 'Steel Scrub Pack', 49, 18, 160, 4 ), array( 'Mosquito Repellent Refill', 89, 48, 135, 3 ),
			),
			'Festive'       => array(
				array( 'Diwali Gift Hamper', 1499, 950, 140, 0.1 ), array( 'Brass Diya Set', 899, 420, 60, 0 ),
				array( 'Scented Candle Set', 599, 260, 80, 0.5 ), array( 'Dry Fruit Box 500g', 899, 640, 25, 1 ),
				array( 'Assorted Mystery Box', 499, null, 30, 1 ), array( 'Rangoli Colour Kit', 299, 120, 45, 1 ),
				array( 'Festive Torans', 249, 90, 40, 1 ), array( 'Gift Wrap Roll', 99, 35, 90, 2 ),
			),
		),
	),
	// Cart recovery demo: hand-picked products with costs, then shoppers with saved carts.
	'catalog-b' => array(
		'seed'     => 202,
		'orders'   => 7,
		'shoppers' => 300,
		'hero'     => array( 'Fresh Deodorant 150ml', 80 ), // product name, carts holding it
		// name, price, cost of goods (null: unknown), how often it lands in a cart
		'products' => array(
			'Fashion' => array(
				array( 'Linen Shirt', 1499, 690, 3 ), array( 'Cotton Kurta', 1199, 520, 2 ), array( 'Denim Jacket', 2999, 1650, 2 ),
				array( 'Silk Scarf', 899, 310, 1 ), array( 'Wool Sweater', 2499, 1400, 1 ), array( 'Handloom Saree', 3999, 2900, 1 ),
				array( 'Chino Trousers', 1799, 880, 1 ), array( 'Canvas Sneakers', 1999, 1150, 2 ),
			),
			'Sports'  => array(
				array( 'Yoga Mat 6mm', 999, 380, 3 ), array( 'Cricket Bat English Willow', 7999, 4200, 1 ), array( 'Football Size 5', 899, 420, 2 ),
				array( 'Badminton Racket', 1499, 760, 1 ), array( 'Dumbbell Pair 5kg', 1899, 1500, 1 ), array( 'Skipping Rope', 299, 90, 2 ),
				array( 'Running Shorts', 699, 280, 1 ), array( 'Gym Gloves', 499, 160, 1 ),
			),
			'Beauty'  => array(
				array( 'Fresh Deodorant 150ml', 249, 110, 0 ), array( 'Vitamin C Serum 30ml', 799, 240, 3 ), array( 'Aloe Face Wash', 249, 95, 2 ),
				array( 'Rose Lip Balm', 149, 45, 1 ), array( 'Herbal Shampoo 400ml', 399, 170, 2 ), array( 'Charcoal Soap Pack', 199, 80, 1 ),
				array( 'Saffron Body Lotion', 549, 260, 1 ), array( 'Sunscreen SPF 50', 499, 210, 2 ),
			),
			'Grocery' => array(
				array( 'Basmati Rice 5kg', 699, 640, 3 ), array( 'Roasted Almonds 500g', 649, 470, 2 ), array( 'Wild Honey 500g', 449, 230, 2 ),
				array( 'Coffee Beans 250g', 599, 290, 2 ), array( 'Cold-Pressed Olive Oil 1L', 1099, 820, 1 ), array( 'Green Tea 100 Bags', 349, 120, 1 ),
				array( 'Dark Chocolate 70%', 249, 130, 1 ), array( 'Organic Jaggery 1kg', 199, 150, 1 ),
			),
			'Garden'  => array(
				array( 'Terracotta Planter Set', 799, 330, 2 ), array( 'Self-Watering Pot', 649, 280, 1 ), array( 'Garden Hose 15m', 1299, 760, 1 ),
				array( 'Pruning Shears', 549, 210, 1 ), array( 'Vegetable Seed Kit', 299, 85, 2 ), array( 'Solar Lantern', 999, 520, 1 ),
				array( 'Coir Grow Bags x10', 399, 150, 1 ), array( 'Mystery Garden Box', 1499, null, 1 ),
			),
		),
	),
);

function seed_product( $name, $price, $category_id, $cogs = null ) {
	$product = new WC_Product_Simple();
	$product->set_name( $name );
	$product->set_regular_price( (string) $price );
	$product->set_status( 'publish' );
	if ( $category_id ) {
		$product->set_category_ids( array( $category_id ) );
	}
	if ( null !== $cogs ) {
		$product->set_cogs_value( (float) $cogs ); // WooCommerce's own Cost of Goods field; needs the feature on
	}
	return $product->save();
}

function seed_history_order( $n, $products, $lines, $age, $status ) {
	$at    = time() - $age;
	$order = wc_create_order();
	foreach ( $lines as $id => $qty ) {
		$order->add_product( $products[ $id ], $qty );
	}
	$order->set_address( array( 'first_name' => 'Demo', 'last_name' => "Customer $n", 'email' => "customer$n@example.com" ), 'billing' );
	$order->calculate_totals();
	$order->set_status( $status );
	$order->set_date_created( $at );
	if ( 'completed' === $status ) {
		$order->set_date_paid( $at );
		$order->set_date_completed( $at );
	}
	$order->update_meta_data( '_synthetic_age', $age ); // wp restock refresh re-dates from this
	$order->save();
}

$template = getenv( 'TEMPLATE' ) ?: 'blank';
$ids      = array();
$by_name  = array();
$weights  = array();
$stocked  = array();

if ( 'blank' === $template ) {
	mt_srand( 303 );
	$orders = 2;
	foreach ( file( '/setup/products', FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES ) as $line ) {
		list( $name, $price ) = array_map( 'trim', explode( '|', $line ) );
		$ids[] = seed_product( $name, $price, 0 );
	}
} elseif ( isset( $catalogs[ $template ] ) ) {
	$catalog = $catalogs[ $template ];
	mt_srand( $catalog['seed'] );
	$orders = $catalog['orders'];
	foreach ( $catalog['products'] ?? array() as $category => $rows ) {
		$term = term_exists( $category, 'product_cat' ) ?: wp_insert_term( $category, 'product_cat' );
		foreach ( $rows as list( $name, $price, $cogs, $weight ) ) {
			$id               = seed_product( $name, $price, (int) $term['term_id'], $cogs );
			$ids[]            = $id;
			$by_name[ $name ] = $id;
			$weights[ $id ]   = $weight;
		}
	}
	foreach ( $catalog['stock_products'] ?? array() as $category => $rows ) {
		$term = term_exists( $category, 'product_cat' ) ?: wp_insert_term( $category, 'product_cat' );
		foreach ( $rows as $row ) {
			list( $name, $price, $cogs, $stock, $per_day ) = $row;
			$id             = seed_product( $name, $price, (int) $term['term_id'], $cogs );
			$ids[]          = $id;
			$stocked[ $id ] = array( $stock, $per_day, $row[5] ?? 0 );
		}
	}
} else {
	WP_CLI::error( "Unknown TEMPLATE '$template': use catalog-a, catalog-b or blank." );
}

for ( $n = 1; $n <= $orders; $n++ ) {
	$order = wc_create_order();
	for ( $k = mt_rand( 1, 3 ); $k > 0; $k-- ) {
		$order->add_product( wc_get_product( $ids[ mt_rand( 0, count( $ids ) - 1 ) ] ), mt_rand( 1, 2 ) );
	}
	$order->set_address( array( 'first_name' => 'Demo', 'last_name' => "Customer $n", 'email' => "customer$n@example.com" ), 'billing' );
	$order->calculate_totals();
	$order->update_status( 'processing' );
}

// Sales history: unit k of a product sold k / per_day days ago (counted from when it sold
// out, if it did), so any window gives the stated rate. Units close in time share an order.
if ( $stocked ) {
	add_filter( 'pre_wp_mail', '__return_false' ); // no "order completed" mail to example.com
	mt_srand( $catalog['seed'] + 2 );
	$horizon = $catalog['history_days'] * DAY_IN_SECONDS;
	$units   = array();
	foreach ( $stocked as $id => list( , $per_day, $stopped ) ) {
		for ( $k = 0; $per_day > 0 && ( $age = (int) ( ( $stopped + $k / $per_day ) * DAY_IN_SECONDS ) ) < $horizon; $k++ ) {
			$units[] = array( $age, $id );
		}
	}
	sort( $units );
	$products = array();
	foreach ( array_keys( $stocked ) as $id ) {
		$products[ $id ] = wc_get_product( $id );
	}
	$n = 0;
	for ( $i = 0; $i < count( $units ); $i += $size ) {
		$size  = mt_rand( 1, 12 );
		$lines = array_count_values( array_column( array_slice( $units, $i, $size ), 1 ) );
		seed_history_order( ++$n, $products, $lines, $units[ $i ][0], 'completed' );
		if ( mt_rand( 1, 100 ) <= 3 ) { // now and then a cancelled order: not a sale, so the rates hold
			seed_history_order( ++$n, $products, $lines, $units[ $i ][0], 'cancelled' );
		}
	}
	// Stock last: with tracking on, WooCommerce would have taken the history's sales out of it.
	foreach ( $stocked as $id => list( $stock ) ) {
		$products[ $id ]->set_manage_stock( true );
		$products[ $id ]->set_stock_quantity( $stock );
		$products[ $id ]->save();
	}
	WP_CLI::log( "$n history orders over {$catalog['history_days']} days" );
}

// Abandoned carts: shoppers with a saved WooCommerce cart (the same user meta
// WooCommerce writes for a logged-in visitor) and the time it last changed.
$shoppers = $catalog['shoppers'] ?? 0;
$holders  = array();
if ( $shoppers ) {
	list( $hero_name, $hero_left ) = $catalog['hero'];
	$hero     = $by_name[ $hero_name ];
	$password = getenv( 'SHOPPER_PASSWORD' ) ?: wp_generate_password( 24 );
	// Own seed: the carts stay the same even if anything above draws a different number of randoms.
	mt_srand( $catalog['seed'] + 1 );
	$cart_key = '_woocommerce_persistent_cart_' . get_current_blog_id();
	$pick     = function () use ( $weights ) {
		$r = mt_rand( 1, array_sum( $weights ) );
		foreach ( $weights as $id => $w ) {
			if ( ( $r -= $w ) <= 0 ) {
				return $id;
			}
		}
	};
	for ( $n = 1; $n <= $shoppers; $n++ ) {
		$login    = sprintf( 'shopper%03d', $n );
		$customer = new WC_Customer();
		$customer->set_username( $login );
		$customer->set_email( "$login@example.com" );
		$customer->set_password( $password );
		$customer->set_first_name( 'Shopper' );
		$customer->set_last_name( sprintf( '%03d', $n ) );
		$customer->set_billing_email( "$login@example.com" );
		$user_id = $customer->save();

		$items = array();
		// Selection sampling: exactly $hero_left of the remaining shoppers get the hero.
		if ( mt_rand( 1, $shoppers - $n + 1 ) <= $hero_left ) {
			$hero_left--;
			$items[ $hero ] = mt_rand( 1, 2 );
			$holders[]      = $login;
		}
		for ( $k = empty( $items ) ? mt_rand( 1, 3 ) : mt_rand( 0, 2 ); $k > 0; $k-- ) {
			$items[ $pick() ] = mt_rand( 1, 2 );
		}
		$cart = array();
		foreach ( $items as $product_id => $quantity ) {
			$key          = md5( (string) $product_id ); // WC_Cart::generate_cart_id for a simple product
			$cart[ $key ] = array( 'key' => $key, 'product_id' => $product_id, 'variation_id' => 0, 'variation' => array(), 'quantity' => $quantity );
		}
		update_user_meta( $user_id, $cart_key, array( 'cart' => $cart ) );
		update_user_meta( $user_id, '_cart_updated_at', time() - cart_recovery_synthetic_age( $n ) ); // mu-plugin cart-recovery.php
		update_user_meta( $user_id, '_synthetic_shopper', 1 );
	}
	WP_CLI::log( count( $holders ) . " carts hold $hero_name, e.g. " . implode( ', ', array_slice( $holders, 0, 5 ) ) );
}

WP_CLI::success( count( $ids ) . " products, $orders orders, $shoppers shoppers with carts ($template)" );
