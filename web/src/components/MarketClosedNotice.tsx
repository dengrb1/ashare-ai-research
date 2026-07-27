import { useMarket } from '../context/MarketContext'

export function MarketClosedNotice() {
  const { marketSession } = useMarket()
  if (marketSession?.state !== 'CLOSED') return null
  return <div className="market-closed-notice" role="status">A 股已收盘，当前展示为收盘后行情。</div>
}
