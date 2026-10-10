<?php
/**
 * Seeds an empty store in one WordPress boot (one wp-cli call per product
 * takes 1-2 s each; 300 of them would not fit the provisioning timeout).
 *
 *   TEMPLATE=catalog-a|catalog-b|blank wp eval-file seed.php
 *
 * catalog-a / catalog-b: 300 products, 60 in each of 5 categories, generated
 * from word lists with a fixed seed, so every store of a template is identical.
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
	'catalog-b' => array(
		'seed'       => 202,
		'orders'     => 7,
		'categories' => array(
			'Fashion' => array( array( 'Linen', 'Cotton', 'Denim', 'Silk', 'Wool', 'Handloom' ), array( 'Shirt', 'Kurta', 'Scarf', 'Jacket', 'Dress', 'Trousers' ), 499, 5999 ),
			'Sports'  => array( array( 'Pro', 'Training', 'Lightweight', 'Grip', 'All-Weather', 'Junior' ), array( 'Cricket Bat', 'Football', 'Yoga Mat', 'Racket', 'Dumbbell', 'Skipping Rope' ), 299, 7999 ),
			'Beauty'  => array( array( 'Herbal', 'Rose', 'Aloe', 'Charcoal', 'Saffron', 'Vitamin C' ), array( 'Face Wash', 'Serum', 'Lip Balm', 'Shampoo', 'Body Lotion', 'Soap' ), 99, 1999 ),
			'Grocery' => array( array( 'Organic', 'Roasted', 'Cold-Pressed', 'Stone-Ground', 'Wild', 'Single-Origin' ), array( 'Basmati Rice', 'Almonds', 'Honey', 'Coffee Beans', 'Olive Oil', 'Green Tea' ), 99, 1499 ),
			'Garden'  => array( array( 'Terracotta', 'Hanging', 'Self-Watering', 'Steel', 'Coir', 'Solar' ), array( 'Planter', 'Hose', 'Pruner', 'Seed Kit', 'Lantern', 'Trowel' ), 149, 3499 ),
		),
	),
);

function seed_product( $name, $price, $category_id ) {
	$product = new WC_Product_Simple();
	$product->set_name( $name );
	$product->set_regular_price( (string) $price );
	$product->set_status( 'publish' );
	if ( $category_id ) {
		$product->set_category_ids( array( $category_id ) );
	}
	return $product->save();
}

$template = getenv( 'TEMPLATE' ) ?: 'blank';
$ids      = array();

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
	foreach ( $catalog['categories'] as $category => $words ) {
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

WP_CLI::success( count( $ids ) . " products, $orders orders ($template)" );
