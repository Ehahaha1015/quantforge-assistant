const configured = import.meta.env.VITE_QF_BINDINGS_API || '/gateway'
if (configured !== '/gateway' && !/^http:\/\/127\.0\.0\.1:(4188|4190|4191|4192)$/.test(configured)) throw new Error('截图服务必须使用本机接口或同源部署入口')
export const chartApi: string = configured
