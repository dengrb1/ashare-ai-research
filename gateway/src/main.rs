mod circuit;
mod config;
mod convert;
mod keypool;
mod metrics;
mod relay;
mod router;
mod sse;

use anyhow::{Context, Result};
use config::GatewayConfig;
use relay::AppState;
use router::{build_router, spawn_health_probes};
use std::net::SocketAddr;
use tokio::net::TcpListener;
use tracing::info;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "ashare_model_gateway=info,tower_http=info".into()),
        )
        .compact()
        .init();

    let (config, config_path) = GatewayConfig::load_with_path()?;
    let address: SocketAddr = config
        .listen
        .parse()
        .context("invalid gateway listen address")?;
    let state = AppState::new(config, config_path)?;
    {
        let runtime = state.snapshot();
        info!(
            address = %address,
            config = %state.config_path.display(),
            channels = runtime.channels.len(),
            configured = state.configured_provider_count(),
            "model gateway starting"
        );
    }
    spawn_health_probes(state.clone());
    let listener = TcpListener::bind(address)
        .await
        .context("failed to bind gateway socket")?;
    axum::serve(listener, build_router(state))
        .with_graceful_shutdown(shutdown_signal())
        .await
        .context("gateway server failed")?;
    Ok(())
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    info!("shutdown signal received");
}
