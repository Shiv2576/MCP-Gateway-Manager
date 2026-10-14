<?php
/**
 * Seeds an empty store in one WordPress boot (one wp-cli call per product
 * takes 1-2 s each; 300 of them would not fit the provisioning timeout).
 *
 *   TEMPLATE=catalog-a|catalog-b|blank wp eval-file seed.php
 *
 * catalog-a: 300 products, 60 in each of 5 categories, generated from word
 * lists with a fixed seed, so every store of a template is identical.
 * catalog-b: the cart recovery demo. 40 products with a cost of goods
 * (WooCommerce's Cost of Goods Sold field) and 300 shoppers (shopper001@example.com ...) with saved carts last
 * changed 0-30 days ago; 80 carts hold Fresh Deodorant 150ml. Same fixed seed,
 * same carts in every store. Shopper password: SHOPPER_PASSWORD, else random.
 * blank: the products in /setup/products ("name|price" lines).
 * Then a few orders created now, so "today's orders" has data.
 * The caller runs this only when the store has no products.
 */

$catalogs = array(
	'catalog-a' => array(
		'seed'       => 101,
		'orders'     => 12,
		'categories' => array(
			'Electronics'    => array( array( 'Wireless', 'Smart', 'Portable', 'Compact', 'Pro', 'USB-C' ), array( 'Headphones', 'Speaker', 'Charger', 'Power Bank', 'Keyboard', 'Mouse', 'Webcam' ), 499, 9999 ),
			'Books'          => array( array( 'Illustrated', 'Pocket', 'Collected', 'Annotated', 'Classic', 'Modern' ), array( 'Poems', 'Cookbook', 'Atlas', 'Novel', 'Field Guide', 'Short Stories' ), 199, 1499 ),
			'Home & Kitchen' => array( array( 'Steel', 'Ceramic', 'Bamboo', 'Cast Iron', 'Glass', 'Copper' ), array( 'Pan', 'Kettle', 'Bowl Set', 'Knife', 'Storage Jar', 'Tray' ), 249, 4999 ),
			'Toys'           => array( array( 'Wooden', 'Magnetic', 'Plush', 'Remote Control', 'Puzzle', 'Musical' ), array( 'Train', 'Blocks', 'Bear', 'Car', 'Robot', 'Kite' ), 149, 2999 ),
			'Office'         => array( array( 'Ergonomic', 'Recycled', 'Leather', 'Minimal', 'Magnetic', 'Desk' ), array( 'Notebook', 'Pen Set', 'Organizer', 'Lamp', 'Whiteboard', 'Stapler' ), 99, 3999 ),
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

$template = getenv( 'TEMPLATE' ) ?: 'blank';
$ids      = array();
$by_name  = array();
$weights  = array();

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
	foreach ( $catalog['categories'] ?? array() as $category => $words ) {
		list( $adjectives, $nouns, $min, $max ) = $words;
		$term = term_exists( $category, 'product_cat' ) ?: wp_insert_term( $category, 'product_cat' );
		$code = strtoupper( substr( preg_replace( '/[^A-Za-z]/', '', $category ), 0, 3 ) );
		for ( $i = 1; $i <= 60; $i++ ) {
			$name  = $adjectives[ mt_rand( 0, count( $adjectives ) - 1 ) ] . ' ' . $nouns[ mt_rand( 0, count( $nouns ) - 1 ) ] . sprintf( ' %s-%02d', $code, $i );
			$ids[] = seed_product( $name, mt_rand( $min, $max ), (int) $term['term_id'] );
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
