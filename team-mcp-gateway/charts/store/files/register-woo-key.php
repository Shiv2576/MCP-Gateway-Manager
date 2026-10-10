<?php
/**
 * Register a WooCommerce REST API key whose value comes from the environment,
 * so the same key can live in a Kubernetes Secret and be read by woo-mcp.
 *
 * Run by the store chart's setup Job (charts/store/templates/setup-job.yaml), or by hand:
 *   WOO_CONSUMER_KEY=ck_<40 hex> WOO_CONSUMER_SECRET=cs_<40 hex> \
 *     wp eval-file register-woo-key.php
 *
 * Safe to run again: it replaces the row it created before.
 * NOT YET RUN AGAINST A LIVE STORE. Check it with `python -m app.check` after.
 */

$key    = getenv( 'WOO_CONSUMER_KEY' );
$secret = getenv( 'WOO_CONSUMER_SECRET' );
if ( ! preg_match( '/^ck_[0-9a-f]{40}$/', (string) $key ) || ! preg_match( '/^cs_[0-9a-f]{40}$/', (string) $secret ) ) {
	WP_CLI::error( 'Need WOO_CONSUMER_KEY=ck_<40 hex> and WOO_CONSUMER_SECRET=cs_<40 hex>.' );
}

$admins = get_users( array( 'role' => 'administrator', 'number' => 1, 'fields' => 'ID' ) );
if ( empty( $admins ) ) {
	WP_CLI::error( 'No administrator user to own the key.' );
}

global $wpdb;
$table       = $wpdb->prefix . 'woocommerce_api_keys';
$description = 'woo-mcp (managed by Helm)';
$wpdb->delete( $table, array( 'description' => $description ) );
$ok = $wpdb->insert(
	$table,
	array(
		'user_id'         => (int) $admins[0],
		'description'     => $description,
		'permissions'     => 'read_write',
		// WooCommerce stores only a hash of the key; the secret is stored as is.
		'consumer_key'    => wc_api_hash( $key ),
		'consumer_secret' => $secret,
		'truncated_key'   => substr( $key, -7 ),
	),
	array( '%d', '%s', '%s', '%s', '%s', '%s' )
);
$ok ? WP_CLI::success( 'API key registered.' ) : WP_CLI::error( 'Insert failed: ' . $wpdb->last_error );
