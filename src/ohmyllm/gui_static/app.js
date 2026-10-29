/* OhMyLLM console front-end. Vanilla, no deps, talks to the local bridge. */
"use strict";

/* provider brand glyphs — paths from simple-icons (CC0-4.0, https://simpleicons.org) */
const PROV_ICONS = {
  "openrouter": ["#94A3B8", "M16.778 1.844v1.919q-.569-.026-1.138-.032-.708-.008-1.415.037c-1.93.126-4.023.728-6.149 2.237-2.911 2.066-2.731 1.95-4.14 2.75-.396.223-1.342.574-2.185.798-.841.225-1.753.333-1.751.333v4.229s.768.108 1.61.333c.842.224 1.789.575 2.185.799 1.41.798 1.228.683 4.14 2.75 2.126 1.509 4.22 2.11 6.148 2.236.88.058 1.716.041 2.555.005v1.918l7.222-4.168-7.222-4.17v2.176c-.86.038-1.611.065-2.278.021-1.364-.09-2.417-.357-3.979-1.465-2.244-1.593-2.866-2.027-3.68-2.508.889-.518 1.449-.906 3.822-2.59 1.56-1.109 2.614-1.377 3.978-1.466.667-.044 1.418-.017 2.278.02v2.176L24 6.014Z"],
  "anthropic": ["#191919", "M17.3041 3.541h-3.6718l6.696 16.918H24Zm-10.6082 0L0 20.459h3.7442l1.3693-3.5527h7.0052l1.3693 3.5528h3.7442L10.5363 3.5409Zm-.3712 10.2232 2.2914-5.9456 2.2914 5.9456Z"],
  "openai": ["#412991", "M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"],
  "google": ["#8E75B2", "M11.04 19.32Q12 21.51 12 24q0-2.49.93-4.68.96-2.19 2.58-3.81t3.81-2.55Q21.51 12 24 12q-2.49 0-4.68-.93a12.3 12.3 0 0 1-3.81-2.58 12.3 12.3 0 0 1-2.58-3.81Q12 2.49 12 0q0 2.49-.96 4.68-.93 2.19-2.55 3.81a12.3 12.3 0 0 1-3.81 2.58Q2.49 12 0 12q2.49 0 4.68.96 2.19.93 3.81 2.55t2.55 3.81"],
  "deepseek": ["#5786FE", "M23.748 4.651c-.254-.124-.364.113-.512.233-.051.04-.094.09-.137.137-.372.397-.806.657-1.373.626-.829-.046-1.537.214-2.163.848-.133-.782-.575-1.248-1.247-1.548-.352-.155-.708-.311-.955-.65-.172-.24-.219-.509-.305-.774-.055-.16-.11-.323-.293-.35-.2-.031-.278.136-.356.276-.313.572-.434 1.202-.422 1.84.027 1.436.633 2.58 1.838 3.393.137.094.172.187.129.323-.082.28-.18.553-.266.833-.055.179-.137.218-.328.14a5.5 5.5 0 0 1-1.737-1.179c-.857-.828-1.631-1.743-2.597-2.46a12 12 0 0 0-.689-.47c-.985-.957.13-1.743.387-1.836.27-.098.094-.433-.778-.428-.872.003-1.67.295-2.687.685a3 3 0 0 1-.465.136 9.6 9.6 0 0 0-2.883-.101c-1.885.21-3.39 1.1-4.497 2.622C.082 8.776-.231 10.854.152 13.02c.403 2.284 1.568 4.175 3.36 5.653 1.857 1.533 3.997 2.284 6.438 2.14 1.482-.085 3.132-.284 4.994-1.86.47.234.962.328 1.78.398.629.058 1.235-.031 1.705-.129.735-.155.684-.836.418-.961-2.155-1.004-1.682-.595-2.112-.926 1.095-1.295 2.768-3.598 3.284-6.733.05-.346.115-.834.108-1.114-.004-.171.035-.238.23-.257a4.2 4.2 0 0 0 1.545-.475c1.397-.763 1.96-2.016 2.093-3.517.02-.23-.004-.467-.247-.588M11.58 18.168c-2.088-1.642-3.101-2.183-3.52-2.16-.39.024-.32.472-.234.763.09.288.207.487.371.74.114.167.192.416-.113.603-.673.416-1.842-.14-1.897-.168-1.361-.801-2.5-1.86-3.301-3.306-.775-1.393-1.225-2.888-1.299-4.482-.02-.385.094-.522.477-.592a4.7 4.7 0 0 1 1.53-.038c2.131.311 3.946 1.264 5.467 2.774.868.86 1.525 1.887 2.202 2.89.72 1.066 1.494 2.082 2.48 2.915.348.291.626.513.892.677-.802.09-2.14.109-3.055-.615zm1.001-6.44a.306.306 0 0 1 .415-.287.3.3 0 0 1 .113.074.3.3 0 0 1 .086.214c0 .17-.136.307-.308.307a.303.303 0 0 1-.306-.307m3.11 1.596c-.2.081-.4.151-.591.16a1.25 1.25 0 0 1-.798-.254c-.274-.23-.47-.358-.551-.758a1.7 1.7 0 0 1 .015-.588c.07-.327-.007-.537-.238-.727-.188-.156-.426-.199-.689-.199a.6.6 0 0 1-.254-.078.253.253 0 0 1-.114-.358 1 1 0 0 1 .192-.21c.356-.202.767-.136 1.146.016.352.144.618.408 1.001.782.392.451.462.576.685.915.176.264.336.536.446.848.066.194-.02.353-.25.45"],
  "opencode": ["#000000", "M22 24H2V0h20zM17 4.8H7v14.4h10z"],
  "mistral": ["#FA520F", "M17.143 3.429v3.428h-3.429v3.429h-3.428V6.857H6.857V3.43H3.43v13.714H0v3.428h10.286v-3.428H6.857v-3.429h3.429v3.429h3.429v-3.429h3.428v3.429h-3.428v3.428H24v-3.428h-3.43V3.429z"],
  "vercel": ["#000000", "m12 1.608 12 20.784H0Z"],
  "alibaba-token-plan": ["#FF6A00", "M3.996 4.517h5.291L8.01 6.324 4.153 7.506a1.668 1.668 0 0 0-1.165 1.601v5.786a1.668 1.668 0 0 0 1.165 1.6l3.857 1.183 1.277 1.807H3.996A3.996 3.996 0 0 1 0 15.487V8.513a3.996 3.996 0 0 1 3.996-3.996m16.008 0h-5.291l1.277 1.807 3.857 1.182c.715.227 1.17.889 1.165 1.601v5.786a1.668 1.668 0 0 1-1.165 1.6l-3.857 1.183-1.277 1.807h5.291A3.996 3.996 0 0 0 24 15.487V8.513a3.996 3.996 0 0 0-3.996-3.996m-4.007 8.345H8.002v-1.804h7.995Z"],
  "alibaba-coding-plan": ["#FF6A00", "M3.996 4.517h5.291L8.01 6.324 4.153 7.506a1.668 1.668 0 0 0-1.165 1.601v5.786a1.668 1.668 0 0 0 1.165 1.6l3.857 1.183 1.277 1.807H3.996A3.996 3.996 0 0 1 0 15.487V8.513a3.996 3.996 0 0 1 3.996-3.996m16.008 0h-5.291l1.277 1.807 3.857 1.182c.715.227 1.17.889 1.165 1.601v5.786a1.668 1.668 0 0 1-1.165 1.6l-3.857 1.183-1.277 1.807h5.291A3.996 3.996 0 0 0 24 15.487V8.513a3.996 3.996 0 0 0-3.996-3.996m-4.007 8.345H8.002v-1.804h7.995Z"],
  "alibaba": ["#FF6A00", "M3.996 4.517h5.291L8.01 6.324 4.153 7.506a1.668 1.668 0 0 0-1.165 1.601v5.786a1.668 1.668 0 0 0 1.165 1.6l3.857 1.183 1.277 1.807H3.996A3.996 3.996 0 0 1 0 15.487V8.513a3.996 3.996 0 0 1 3.996-3.996m16.008 0h-5.291l1.277 1.807 3.857 1.182c.715.227 1.17.889 1.165 1.601v5.786a1.668 1.668 0 0 1-1.165 1.6l-3.857 1.183-1.277 1.807h5.291A3.996 3.996 0 0 0 24 15.487V8.513a3.996 3.996 0 0 0-3.996-3.996m-4.007 8.345H8.002v-1.804h7.995Z"],
  "moonshot": ["#000000", "m1.053 16.91 9.538 2.55a21 20.981 0 0 0 .06 2.031l5.956 1.592a12 11.99 0 0 1-15.554-6.172m-1.02-5.79 11.352 3.035a21 20.981 0 0 0-.469 2.01l10.817 2.89a12 11.99 0 0 1-1.845 2.004L.658 15.918a12 11.99 0 0 1-.625-4.796m1.593-5.146L13.573 9.17a21 20.981 0 0 0-1.01 1.874l11.297 3.02a21 20.981 0 0 1-.67 2.362l-11.55-3.087L.125 10.26a12 11.99 0 0 1 1.499-4.285ZM6.067 1.58l11.285 3.016a21 20.981 0 0 0-1.688 1.719l7.824 2.091a21 20.981 0 0 1 .513 2.664L2.107 5.218a12 11.99 0 0 1 3.96-3.638M21.68 4.866 7.222 1.003A12 11.99 0 0 1 21.68 4.866"],
  "nvidia": ["#76B900", "M8.948 8.798v-1.43a6.7 6.7 0 0 1 .424-.018c3.922-.124 6.493 3.374 6.493 3.374s-2.774 3.851-5.75 3.851c-.398 0-.787-.062-1.158-.185v-4.346c1.528.185 1.837.857 2.747 2.385l2.04-1.714s-1.492-1.952-4-1.952a6.016 6.016 0 0 0-.796.035m0-4.735v2.138l.424-.027c5.45-.185 9.01 4.47 9.01 4.47s-4.08 4.964-8.33 4.964c-.37 0-.733-.035-1.095-.097v1.325c.3.035.61.062.91.062 3.957 0 6.82-2.023 9.593-4.408.459.371 2.34 1.263 2.73 1.652-2.633 2.208-8.772 3.984-12.253 3.984-.335 0-.653-.018-.971-.053v1.864H24V4.063zm0 10.326v1.131c-3.657-.654-4.673-4.46-4.673-4.46s1.758-1.944 4.673-2.262v1.237H8.94c-1.528-.186-2.73 1.245-2.73 1.245s.68 2.412 2.739 3.11M2.456 10.9s2.164-3.197 6.5-3.533V6.201C4.153 6.59 0 10.653 0 10.653s2.35 6.802 8.948 7.42v-1.237c-4.84-.6-6.492-5.936-6.492-5.936z"],
  "github": ["#181717", "M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"],
  "huggingface": ["#FFD21E", "M12.025 1.13c-5.77 0-10.449 4.647-10.449 10.378 0 1.112.178 2.181.503 3.185.064-.222.203-.444.416-.577a.96.96 0 0 1 .524-.15c.293 0 .584.124.84.284.278.173.48.408.71.694.226.282.458.611.684.951v-.014c.017-.324.106-.622.264-.874s.403-.487.762-.543c.3-.047.596.06.787.203s.31.313.4.467c.15.257.212.468.233.542.01.026.653 1.552 1.657 2.54.616.605 1.01 1.223 1.082 1.912.055.537-.096 1.059-.38 1.572.637.121 1.294.187 1.967.187.657 0 1.298-.063 1.921-.178-.287-.517-.44-1.041-.384-1.581.07-.69.465-1.307 1.081-1.913 1.004-.987 1.647-2.513 1.657-2.539.021-.074.083-.285.233-.542.09-.154.208-.323.4-.467a1.08 1.08 0 0 1 .787-.203c.359.056.604.29.762.543s.247.55.265.874v.015c.225-.34.457-.67.683-.952.23-.286.432-.52.71-.694.257-.16.547-.284.84-.285a.97.97 0 0 1 .524.151c.228.143.373.388.43.625l.006.04a10.3 10.3 0 0 0 .534-3.273c0-5.731-4.678-10.378-10.449-10.378M8.327 6.583a1.5 1.5 0 0 1 .713.174 1.487 1.487 0 0 1 .617 2.013c-.183.343-.762-.214-1.102-.094-.38.134-.532.914-.917.71a1.487 1.487 0 0 1 .69-2.803m7.486 0a1.487 1.487 0 0 1 .689 2.803c-.385.204-.536-.576-.916-.71-.34-.12-.92.437-1.103.094a1.487 1.487 0 0 1 .617-2.013 1.5 1.5 0 0 1 .713-.174m-10.68 1.55a.96.96 0 1 1 0 1.921.96.96 0 0 1 0-1.92m13.838 0a.96.96 0 1 1 0 1.92.96.96 0 0 1 0-1.92M8.489 11.458c.588.01 1.965 1.157 3.572 1.164 1.607-.007 2.984-1.155 3.572-1.164.196-.003.305.12.305.454 0 .886-.424 2.328-1.563 3.202-.22-.756-1.396-1.366-1.63-1.32q-.011.001-.02.006l-.044.026-.01.008-.03.024q-.018.017-.035.036l-.032.04a1 1 0 0 0-.058.09l-.014.025q-.049.088-.11.19a1 1 0 0 1-.083.116 1.2 1.2 0 0 1-.173.18q-.035.029-.075.058a1.3 1.3 0 0 1-.251-.243 1 1 0 0 1-.076-.107c-.124-.193-.177-.363-.337-.444-.034-.016-.104-.008-.2.022q-.094.03-.216.087-.06.028-.125.063l-.13.074q-.067.04-.136.086a3 3 0 0 0-.135.096 3 3 0 0 0-.26.219 2 2 0 0 0-.12.121 2 2 0 0 0-.106.128l-.002.002a2 2 0 0 0-.09.132l-.001.001a1.2 1.2 0 0 0-.105.212q-.013.036-.024.073c-1.139-.875-1.563-2.317-1.563-3.203 0-.334.109-.457.305-.454m.836 10.354c.824-1.19.766-2.082-.365-3.194-1.13-1.112-1.789-2.738-1.789-2.738s-.246-.945-.806-.858-.97 1.499.202 2.362c1.173.864-.233 1.45-.685.64-.45-.812-1.683-2.896-2.322-3.295s-1.089-.175-.938.647 2.822 2.813 2.562 3.244-1.176-.506-1.176-.506-2.866-2.567-3.49-1.898.473 1.23 2.037 2.16c1.564.932 1.686 1.178 1.464 1.53s-3.675-2.511-4-1.297c-.323 1.214 3.524 1.567 3.287 2.405-.238.839-2.71-1.587-3.216-.642-.506.946 3.49 2.056 3.522 2.064 1.29.33 4.568 1.028 5.713-.624m5.349 0c-.824-1.19-.766-2.082.365-3.194 1.13-1.112 1.789-2.738 1.789-2.738s.246-.945.806-.858.97 1.499-.202 2.362c-1.173.864.233 1.45.685.64.451-.812 1.683-2.896 2.322-3.295s1.089-.175.938.647-2.822 2.813-2.562 3.244 1.176-.506 1.176-.506 2.866-2.567 3.49-1.898-.473 1.23-2.037 2.16c-1.564.932-1.686 1.178-1.464 1.53s3.675-2.511 4-1.297c.323 1.214-3.524 1.567-3.287 2.405.238.839 2.71-1.587 3.216-.642.506.946-3.49 2.056-3.522 2.064-1.29.33-4.568 1.028-5.713-.624"],
  "perplexity": ["#1FB8CD", "M22.3977 7.0896h-2.3106V.0676l-7.5094 6.3542V.1577h-1.1554v6.1966L4.4904 0v7.0896H1.6023v10.3976h2.8882V24l6.932-6.3591v6.2005h1.1554v-6.0469l6.9318 6.1807v-6.4879h2.8882V7.0896zm-3.4657-4.531v4.531h-5.355l5.355-4.531zm-13.2862.0676 4.8691 4.4634H5.6458V2.6262zM2.7576 16.332V8.245h7.8476l-6.1149 6.1147v1.9723H2.7576zm2.8882 5.0404v-3.8852h.0001v-2.6488l5.7763-5.7764v7.0111l-5.7764 5.2993zm12.7086.0248-5.7766-5.1509V9.0618l5.7766 5.7766v6.5588zm2.8882-5.0652h-1.733v-1.9723L13.3948 8.245h7.8478v8.087z"],
  "minimax": ["#E73562", "M11.43 3.92a.86.86 0 1 0-1.718 0v14.236a1.999 1.999 0 0 1-3.997 0V9.022a.86.86 0 1 0-1.718 0v3.87a1.999 1.999 0 0 1-3.997 0V11.49a.57.57 0 0 1 1.139 0v1.404a.86.86 0 0 0 1.719 0V9.022a1.999 1.999 0 0 1 3.997 0v9.134a.86.86 0 0 0 1.719 0V3.92a1.998 1.998 0 1 1 3.996 0v11.788a.57.57 0 1 1-1.139 0zm10.572 3.105a2 2 0 0 0-1.999 1.997v7.63a.86.86 0 0 1-1.718 0V3.923a1.999 1.999 0 0 0-3.997 0v16.16a.86.86 0 0 1-1.719 0V18.08a.57.57 0 1 0-1.138 0v2a1.998 1.998 0 0 0 3.996 0V3.92a.86.86 0 0 1 1.719 0v12.73a1.999 1.999 0 0 0 3.996 0V9.023a.86.86 0 1 1 1.72 0v6.686a.57.57 0 0 0 1.138 0V9.022a2 2 0 0 0-1.998-1.997"],
};

/* dark-theme legibility: near-black brand marks (GitHub, Vercel, Anthropic,
 * Moonshot) render as white instead of vanishing into the surface */
function _logoColor(hex) {
  const n = parseInt(hex.slice(1), 16);
  const lum = 0.2126 * ((n >> 16) & 255) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255);
  return lum < 96 ? "#e8e8e8" : hex;
}

const BOOT = window.__OMLLM__ || { token: "", port: 0 };
// the boot URL (?t=…) only authenticates the first HTML fetch; the token is
// already injected into the page — scrub the query so it never lingers in
// browser history / the address bar
if (location.search && BOOT.token) { history.replaceState(null, "", location.pathname); }
const BASE = `http://127.0.0.1:${BOOT.port}`;
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  view: "chat",
  status: null,
  config: null,
  session: null,
  streaming: false,
  approvalQueue: [],
  modeModal: false,
};

/* ------------------------------------------------------------------ api */
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", "X-OMLLM-Token": BOOT.token };
  const res = await fetch(BASE + path, { ...opts, headers });
  if (!res.ok && res.status === 401) throw new Error("bridge token rejected — restart the GUI");
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
const get = (p) => api(p);
const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });
const patch = (p, body) => api(p, { method: "PATCH", body: JSON.stringify(body || {}) });
const del = (p) => api(p, { method: "DELETE" });

function promptModal(title, hint, secret, def) {
  return new Promise((resolve) => {
    const m = $("#prompt-modal"), inp = $("#pm-input");
    $("#pm-title").textContent = title;
    const hintEl = $("#pm-hint");
    hintEl.innerHTML = "";
    if (hint) {
      const span = document.createElement("span");
      span.textContent = hint + "  ";
      hintEl.appendChild(span);
      const link = hint.match(/https:\/\/[^\s)]+/);
      if (link) {
        const a = document.createElement("a");
        a.href = link[0]; a.target = "_blank"; a.rel = "noopener"; a.textContent = "open";
        hintEl.appendChild(a);
      }
    }
    inp.type = secret ? "password" : "text";
    inp.value = def || "";
    m.hidden = false; $("#scrim").hidden = false;
    inp.focus();
    const done = (val) => { m.hidden = true; $("#scrim").hidden = true; $("#pm-ok").onclick = null; $("#pm-cancel").onclick = null; inp.onkeydown = null; resolve(val); };
    $("#pm-ok").onclick = () => done(inp.value.trim());
    $("#pm-cancel").onclick = () => done(null);
    inp.onkeydown = (e) => { if (e.key === "Enter") done(inp.value.trim()); if (e.key === "Escape") done(null); };
  });
}

function confirmModal(title, body, okLabel = "Confirm") {
  return new Promise((resolve) => {
    const m = $("#confirm-modal");
    $("#cf-title").textContent = title;
    $("#cf-body").textContent = body;
    $("#cf-ok").textContent = okLabel;
    m.hidden = false; $("#scrim").hidden = false;
    $("#cf-ok").focus();
    const done = (val) => { m.hidden = true; $("#scrim").hidden = true; $("#cf-ok").onclick = null; $("#cf-cancel").onclick = null; m.onkeydown = null; resolve(val); };
    $("#cf-ok").onclick = () => done(true);
    $("#cf-cancel").onclick = () => done(false);
    m.onkeydown = (e) => { if (e.key === "Escape") done(false); if (e.key === "Enter") done(true); };
  });
}

function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 4200);
}

/* -------------------------------------------------------------- markdown */
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
const KEYWORDS = /\b(fn|def|class|const|let|var|use|mod|pub|struct|enum|impl|if|else|elif|for|while|return|import|from|async|await|try|except|catch|throw|new|match|switch|case|break|continue|self|this|null|None|nil|true|false|True|False)\b/g;
function hl(code, lang) {
  if (/^(json|toml|ini|yaml)$/.test(lang)) return esc(code);
  let out = esc(code);
  out = out.replace(/(&quot;.*?&quot;|&#x27;.*?&#x27;|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/g, '<span class="str">$1</span>');
  out = out.replace(/(\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/)/g, (m) => (m.includes("class=\"") ? m : `<span class="cmt">${m}</span>`));
  out = out.replace(KEYWORDS, '<span class="kw">$1</span>');
  out = out.replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="num">$1</span>');
  return out;
}
function inline(s) {
  s = esc(s);
  s = s.replace(/`([^`]+)`/g, (m, c) => `<code>${c}</code>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/(^|\W)\*([^*\s][^*]*)\*/g, "$1<i>$2</i>");
  s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s;
}
function md(src) {
  const blocks = [];
  let text = String(src).replace(/```(\w*)\n([\s\S]*?)```/g, (m, lang, code) => {
    const i = blocks.push({ lang: lang || "text", code }) - 1;
    return `\u0000B${i}\u0000`;
  });
  const lines = text.split("\n");
  let html = "", list = null, para = [];
  const flushPara = () => { if (para.length) { html += `<p>${inline(para.join(" "))}</p>`; para = []; } };
  const flushList = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    const bm = line.match(/^\u0000B(\d+)\u0000$/);
    if (bm) {
      flushPara(); flushList();
      const b = blocks[+bm[1]];
      html += `<pre class="codeblock"><code>${hl(b.code, b.lang)}</code><button class="btn small copy-code" data-code="${esc(b.code)}">copy</button></pre>`;
      continue;
    }
    const h = line.match(/^(#{1,3})\s+(.*)/);
    if (h) { flushPara(); flushList(); html += `<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`; continue; }
    const ul = line.match(/^[-*]\s+(.*)/);
    const ol = line.match(/^\d+[.)]\s+(.*)/);
    if (ul || ol) {
      flushPara();
      const want = ul ? "ul" : "ol";
      if (list !== want) { flushList(); html += `<${want}>`; list = want; }
      html += `<li>${inline((ul || ol)[1])}</li>`; continue;
    }
    if (/^>\s?/.test(line)) { flushPara(); flushList(); html += `<blockquote>${inline(line.replace(/^>\s?/, ""))}</blockquote>`; continue; }
    if (!line.trim()) { flushPara(); flushList(); continue; }
    para.push(line);
  }
  flushPara(); flushList();
  return html;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest(".copy-code");
  if (!b) return;
  navigator.clipboard.writeText(b.dataset.code).then(() => toast("copied"));
});

/* ----------------------------------------------------------------- chat */
function clearStream() { $("#stream").innerHTML = ""; }

function emptyState() {
  clearStream();
  $("#stream").innerHTML = `
    <div class="stream-empty">
      <svg viewBox="0 0 48 48"><circle cx="24" cy="24" r="16"/><circle cx="24" cy="24" r="6"/></svg>
      <p class="big">Ask the agent anything.</p>
      <p>It reads its <b>soul.md</b> memory, runs <b>permission-gated tools</b>, and everything stays on this machine.</p>
      <p class="small">Try: <button class="btn small remember-demo">"remember my name is Minas"</button> or press <kbd>Ctrl</kbd>+<kbd>K</kbd> for commands.</p>
    </div>`;
  const d = $(".remember-demo");
  if (d) d.onclick = () => { $("#input").value = "remember: my name is Minas"; $("#input").focus(); };
}

function addMsg(role, content, extraClass = "") {
  if (document.querySelector(".stream-empty")) clearStream();
  const el = document.createElement("div");
  el.className = `msg ${role} ${extraClass}`;
  el.innerHTML = `<div class="who">${role === "user" ? "you" : role === "assistant" ? "agent" : "note"}</div><div class="content md"></div>`;
  el.querySelector(".content").innerHTML = role === "user" ? userHtml(content) : md(content);
  $("#stream").appendChild(el);
  scrollBottom();
  return el.querySelector(".content");
}
function userHtml(content) {
  // user messages may be multimodal parts (agent-side images from read_file)
  if (Array.isArray(content)) {
    const txt = content.filter((p) => p && p.type === "text").map((p) => inline(p.text || "")).join("<br>");
    const imgs = content
      .filter((p) => p && p.type === "image_url" && String((p.image_url || {}).url || "").startsWith("data:"))
      .map((p) => `<img class="msg-img" src="${p.image_url.url}" alt="attached image" loading="lazy">`)
      .join("");
    return (txt || "") + (imgs ? `<div class="msg-imgs">${imgs}</div>` : "");
  }
  return inline(content);
}
function addEvline(tag, text, cls = "", dur) {
  const el = document.createElement("div");
  el.className = `evline ${cls}`;
  el.innerHTML = `<span class="tag">${esc(tag)}</span><span class="txt"></span>` + (dur ? `<span class="dur">${esc(dur)}</span>` : "");
  el.querySelector(".txt").textContent = text;
  $("#stream").appendChild(el);
  scrollBottom();
  return el;
}
function scrollBottom(force) {
  const s = $("#stream");
  // follow the stream only while the user is at the bottom; scrolling up to
  // read must not be yanked back by the next token
  if (force || s.scrollHeight - s.scrollTop - s.clientHeight < 48) s.scrollTop = s.scrollHeight;
}

let liveNode = null, liveText = "";
function ensureLive() {
  if (!liveNode) { liveNode = addMsg("assistant", ""); }
  return liveNode;
}
function updateLive(chunk) {
  liveText += chunk;
  ensureLive().innerHTML = md(liveText);
  scrollBottom();
}
function sealLive() { liveNode = null; liveText = ""; sealThinkLive(); }
function liveThink(t) {
  state.reasonBuf = (state.reasonBuf || "") + t;
  if (state.config && state.config.agent && state.config.agent.thinking_visible === false) {
    const k = Math.max(1, Math.round(state.reasonBuf.length / 100) / 10);
    const label = `💭 thinking · hidden · ${k}k chars`;
    if (!state.thinkNode || state.thinkNode === "sealed") {
      state.thinkNode = { hidden: true, el: addEvline("think", label, "dim") };
    } else if (state.thinkNode.el) {
      state.thinkNode.el.querySelector(".txt").textContent = label;
    }
    scrollBottom();
    return;
  }
  if (!state.thinkNode || state.thinkNode === "sealed") {
    const det = document.createElement("details"); det.open = true; det.className = "think-details";
    const sm = document.createElement("summary"); sm.textContent = "💭 " + thinkVerb() + "…";
    const pre = document.createElement("pre"); pre.className = "think-body";
    det.append(sm, pre); $("#stream").append(det);
    state.thinkNode = { det, sm, pre };
  }
  state.thinkNode.pre.textContent += t;
  scrollBottom();
}
function liveFileProgress(d) {
  let partial;
  if (d.content_tail) {
    let raw = d.content_tail;
    const cm = /^((?:[^"\\]|\\.)*)"/.exec(raw);   // final event carries the JSON close
    if (cm) raw = cm[1];
    partial = raw.replace(/\\n/g, "\n").replace(/\\"/g, '"');
  } else {
    const m = /"content"\s*:\s*"((?:[^"\\]|\\.)*)$/.exec(d.tail || "");
    if (!m) return;
    partial = m[1].replace(/\\n/g, "\n").replace(/\\"/g, '"');
  }
  const lines = partial.split("\n").slice(-12).join("\n");
  if (!state.fileNode || state.fileNode.name !== d.name) {
    const det = document.createElement("details"); det.open = true; det.className = "think-details";
    const verb = d.name === "patch_file" ? "patching" : "writing";
    const sm = document.createElement("summary"); sm.textContent = `${actionOf(d.name)[1]} ${verb} ${d.name}…`;
    const pre = document.createElement("pre"); pre.className = "think-body";
    det.append(sm, pre); $("#stream").append(det);
    state.fileNode = { name: d.name, det, sm, pre };
  }
  state.fileNode.pre.textContent = lines;
  scrollBottom();
}
const VERBS = ['pondering', 'ruminating', 'contemplating', 'deliberating', 'thinking hard', 'finding x', 'divining', 'untangling', 'synapsing', 'connecting dots', 'reading tea leaves', 'consulting the oracle', 'vibing', 'crunching', 'warming up', 'spinning up', 'cracking knuckles', 'squinting', 'deep-diving', 'following the white rabbit', 'asking the void', 'feeding the hamster', 'oiling the gears', 'summoning context', 'unfolding', 'digesting', 'reflecting', 'brooding', 'scheming', 'daydreaming', 'muttering', 'pacing', 'counting to ten', 'checking assumptions', 'double-checking', 'triple-checking', 'brainstorming', 'whiteboarding', 'rubber-ducking', 'reading the fine print', 'hitting the books', 'putting on glasses', 'sharpening pencils', 'unrolling the map', 'stirring the pot', 'steeping the tea', 'spinning the globe', 'flipping pages', 'cross-referencing', 'triangulating', 'extrapolating', 'hypothesizing', 'theorizing', 'overthinking', 'second-guessing', 'third-guessing', 'panicking (productively)', 'sweating', 'calculating', 'estimating', 'gesturing vaguely', 'nodding slowly', 'stroking the beard', 'pondering the orb', 'questioning reality', "asking \"but why?\"", "asking \"and then what?\"", 'stacking context', 'tidying the desk', 'brewing', 'marinating', 'buffering dramatically', 'channeling the machine spirit', 'negotiating with the tokens'];
function _pick(w) {
  let h = Math.imul(w + 1, 2654435761) >>> 0;
  h = (h ^ (h >>> 13)) >>> 0;
  return VERBS[h % VERBS.length];
}
function thinkVerb() {
  const w = Math.floor(Date.now() / 2700);
  let v = _pick(w);
  if (v === _pick(w - 1)) v = _pick(w + 1);  // never twice in a row
  return v;
}
setInterval(() => {
  const n = state.thinkNode;
  if (n && n !== "sealed" && n.sm) n.sm.textContent = "💭 " + thinkVerb() + "…";
}, 1000);
const ACTION = {
  write_file: ["write", "📝"], patch_file: ["edit", "✏️"], run_command: ["exec", "⚡"],
  read_file: ["read", "📖"], list_dir: ["ls", "📂"], glob: ["find", "🔎"], grep: ["find", "🔎"],
  web_fetch: ["net", "🌐"], web_search: ["net", "🌐"], soul_write: ["memory", "🧠"],
  soul_remember: ["memory", "🧠"], soul_forget: ["memory", "🧠"],
};
function actionOf(name) { return ACTION[name] || ["tool", "🔧"]; }
function planSummary(d) {
  const a = d.args || {};
  switch (d.name) {
    case "run_command": return `⚡ run_command ${firstLine(a.command || "")}`;
    case "write_file": return `📝 write_file ${a.path || "?"} · ${String(a.content || "").length} chars${a.append ? " (append)" : ""}`;
    case "patch_file": {
      const o = String(a.old_text || "").split("\n").length, n = String(a.new_text || "").split("\n").length;
      return `✏️ patch_file ${a.path || "?"} · −${o} +${n} lines`;
    }
    case "read_file": return `📖 read_file ${a.path || "?"}`;
    default: return `→ ${d.name} ${shortArgs(a)}`;
  }
}
function liveThinkProgress(name, chars) {
  const k = ((chars / 1000) | 0) || 1;
  if (state.writeNode && state.writeNode.name === name) {
    state.writeNode.el.querySelector(".txt").textContent = `${actionOf(name)[1]} ${name} … ${k}k chars`;
  } else {
    state.writeNode = { name, el: addEvline(actionOf(name)[0], `${actionOf(name)[1]} ${name} … ${k}k chars`, "dim") };
  }
}
function sealThinkLive() {
  if (state.thinkNode && state.thinkNode !== "sealed") {
    if (state.thinkNode.hidden) { state.thinkNode = "sealed"; return; }
    if (!state.thinkNode.pre.textContent.trim()) {
      state.thinkNode.det.remove();
      state.thinkNode = null;
    } else {
      state.thinkNode.det.open = false;
      state.thinkNode.sm.textContent = "💭 thinking process";
      state.thinkNode = "sealed";
    }
  }
}

async function loadSessionIntoView(sid) {
  const sess = await get(`/api/chats/${sid}`);
  state.session = sid;
  clearStream();
  (sess.messages || []).forEach((m) => {
    if (m.role === "user") addMsg("user", m.content);
    else if (m.role === "assistant") addMsg("assistant", m.content);
    else if (m.role === "tool") addEvline(m.name || "tool", String(m.content).slice(0, 220), "ok");
    else if (m.role === "system") { /* soul context — not shown */ }
  });
  if (!sess.messages?.length) emptyState();
  $("#g-title").textContent = sess.title;
}

/* SSE reader over fetch (POST streaming). */
async function sendToAgent(text, memoryDirective) {
  const body = JSON.stringify({ text, memory_directive: !!memoryDirective });
  let res;
  try {
    res = await fetch(`${BASE}/api/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-OMLLM-Token": BOOT.token },
      body,
    });
  } catch (e) { toast(`bridge unreachable: ${e.message}`, "err"); return false; }
  if (res.status === 202) { toast("queued into the running turn", ""); return true; }
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    toast(d.error || "request failed", "err");
    return false;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
      const ev = (frame.match(/^event: (.*)$/m) || [])[1] || "message";
      const dl = (frame.match(/^data: (.*)$/m) || [])[1] || "{}";
      handleEvent(ev, JSON.parse(dl));
      if (ev === "done") return true;
    }
  }
  return true;
}

function handleEvent(ev, d) {
  switch (ev) {
    case "token": updateLive(d.text || ""); break;
    case "assistant_note": {
      const nt = (d.text || "").trim();
      if (nt && nt !== liveText.trim()) addMsg("systemish", nt, "systemish");
      sealLive();
      break;
    }
    case "tool_plan": {
      sealLive();
      const cls = d.allowed ? "" : "err";
      const el = addEvline(`${(d.level || "low").toLowerCase()}/L${d.risk ?? "?"}`, planSummary(d), cls);
      if (d.args && Object.keys(d.args).length) {
        const det = document.createElement("details"); det.className = "args-det";
        const sm = document.createElement("summary"); sm.textContent = "args";
        const pre = document.createElement("pre"); pre.textContent = JSON.stringify(d.args, null, 1);
        det.append(sm, pre); el.appendChild(det);
      }
      pushToolLog(d.name, planSummary(d), "run");
      break;
    }
    case "tool_result": {
      if (state.fileNode) { state.fileNode.det.remove(); state.fileNode = null; }
      const cls = d.ok ? "ok" : "err";
      const tag = d.blocked ? "blocked" : d.denied ? "denied" : d.ok ? "ok" : "fail";
      const f = d.file;
      const isWrite = f && (f.created != null || f.added != null || f.removed != null || f.changed != null);
      const summary = f && d.ok && isWrite
        ? (f.created ? `📄 created ${f.path}  +${f.added || 0}` : `✏️ edited ${f.path}  -${f.removed || 0} ~${f.changed || 0} +${f.added || 0}`)
        : f && d.ok && f.media
          ? `${({ image: "🖼️", audio: "🔊", video: "🎬" })[f.media] || "📎"} read ${f.media}` +
            (f.images ? ` · ${f.images} image${f.images > 1 ? "s" : ""} attached to the model` : " · transcribed to text")
          : `${d.name}: ${firstLine(d.output)}`;
      addEvline(tag, summary, cls, d.duration_ms != null ? `${Math.round(d.duration_ms)} ms` : "");
      pushToolLog(d.name, tag, d.ok ? "ok" : "err");
      break;
    }
    case "memory_activity": toast(`memory: ${d.tool}`, d.ok ? "" : "err"); pulsePulse(); break;
    case "tool_stream": liveThinkProgress(d.name, d.chars); liveFileProgress(d); break;
    case "compaction": addEvline("ctx", `🗜 compacted ${d.before}→${d.after} messages`, "dim"); break;
    case "interjection": addMsg("user", d.text || ""); break;
    case "ultrathink_review": liveText = ""; addEvline("ultra", "🌀 reviewing draft, re-deriving, rewriting…", "dim"); break;
    case "agentloop_audit": liveText = ""; addEvline("ultra", "🔁 auditing completion — evidence required before the answer…", "dim"); break;
    case "retry": addEvline("retry", d.reason || "", "warn"); break;
    case "thinking": state.reasonBuf = ""; state.thinkNode = null; addEvline("think", `💭 ${thinkVerb()} (step ${d.step || "?"})`, "dim"); break;
    case "thinking_text": liveThink(d.text || ""); break;
    case "error": sealLive(); addEvline("error", d.message || "", "err"); break;
    case "approval_request": queueApproval(d); break;
    case "ping": break;
    case "done": {
      sealLive();
      state.streaming = false;
      $("#cancel").hidden = true; $("#send").disabled = false;
      if (d.error) addEvline("error", d.error, "err");
      const rtext = (state.thinkNode === "sealed" || state.thinkNode) ? "" : (d.reasoning || state.reasonBuf || "");
      if (rtext.trim()) {
        const det = document.createElement("details"); det.className = "think-details";
        const sm = document.createElement("summary"); sm.textContent = "💭 thinking process";
        const bd = document.createElement("div"); bd.className = "think-body"; bd.textContent = rtext;
        det.append(sm, bd); $("#stream").append(det); scrollBottom();
      }
      state.reasonBuf = "";
      const bits = [d.model || "", d.usage?.eval_count ? `${d.usage.eval_count} tok` : "", d.ttft_s ? `ttft ${d.ttft_s.toFixed(1)}s` : "", d.tokens_per_s ? `${d.tokens_per_s.toFixed(1)} tok/s` : "", d.steps ? `${d.steps} step${d.steps > 1 ? "s" : ""}` : "", d.calls?.length ? `${d.calls.length} tool call${d.calls.length > 1 ? "s" : ""}` : ""].filter(Boolean).join(" · ");
      if (bits) addEvline("turn", bits, "ok");
      refreshStatus();
      break;
    }
  }
}
function firstLine(s) { const x = String(s || "").trim().split("\n")[0]; return x.length > 140 ? x.slice(0, 140) + "…" : x; }
function shortArgs(a) {
  try { const s = JSON.stringify(a || {}); return s.length > 90 ? s.slice(0, 90) + "…" : s; } catch { return ""; }
}
function pushToolLog(name, detail, kind) {
  const ul = $("#tools-log");
  if (ul.firstChild?.classList?.contains("muted")) ul.innerHTML = "";
  const li = document.createElement("li");
  li.className = kind;
  li.innerHTML = `<b>${esc(name)}</b> ${esc(String(detail).replace(/[{}"]+/g, " ").slice(0, 52))}`;
  ul.prepend(li);
  while (ul.children.length > 30) ul.lastChild.remove();
}
function pulsePulse() { const p = $("#backend-dot"); p.classList.add("on"); }

/* ------------------------------------------------------------ approvals */
function queueApproval(d) {
  state.approvalQueue.push(d);
  if (state.approvalQueue.length === 1) showApproval(d);
}
function showApproval(d) {
  $("#ap-risk").textContent = `${(d.level || "low").toUpperCase()} · risk ${d.risk ?? "?"}`;
  $("#ap-risk").className = `risk-badge ${d.level === "critical" || d.level === "high" ? "exec" : d.level === "medium" ? "write" : "read"}`;
  $("#ap-tool").textContent = d.tool;
  $("#ap-mode").textContent = ` — mode: ${d.mode}`;
  $("#ap-args").textContent = JSON.stringify(d.args, null, 1);
  $("#ap-reasons").textContent = (d.reasons || []).join(" · ");
  const prev = $("#ap-preview");
  const previewText = d.preview || "";
  if (previewText) {
    prev.hidden = false;
    prev.classList.add("diff");
    prev.innerHTML = previewText.split("\n").map((l) => {
      const e = esc(l);
      if (l.startsWith("+")) return `<span class="add">${e}</span>`;
      if (l.startsWith("-")) return `<span class="del">${e}</span>`;
      if (l.startsWith("@@")) return `<span class="hunk">${e}</span>`;
      return e;
    }).join("\n");
  } else prev.hidden = true;
  openModal("#approval-modal");
}
function closeApproval() {
  closeModal("#approval-modal");
  state.approvalQueue.shift();
  if (state.approvalQueue.length) showApproval(state.approvalQueue[0]);
}
async function answerApproval(decision) {
  const d = state.approvalQueue[0];
  if (!d) return;
  try { await post(`/api/approval/${d.id}`, { decision }); }
  catch (e) { toast(e.message, "err"); }
  closeApproval();
}
["ap-allow", "ap-session", "ap-always", "ap-deny"].forEach((id) => {
  document.getElementById(id).onclick = () => answerApproval(
    ({ "ap-allow": "approve", "ap-session": "session", "ap-always": "always", "ap-deny": "deny" })[id]);
});

/* --------------------------------------------------------------- modals */
function openModal(sel) { $("#scrim").hidden = false; $(sel).hidden = false; $(sel).querySelector("button, input, textarea")?.focus(); }
function closeModal(sel) { $(sel).hidden = true; if (!$$(".modal").some((m) => !m.hidden)) $("#scrim").hidden = true; }
$("#scrim").onclick = () => { if (state.approvalQueue.length) answerApproval("deny"); $$(".modal").forEach((m) => (m.hidden = true)); $("#scrim").hidden = true; };

/* ---------------------------------------------------------------- views */
function switchView(v) {
  if (v === state.view && v !== "chats") return;
  state.view = v;
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach((s) => (s.hidden = s.id !== `view-${v}`));
  const titles = { chat: "Chat", chats: "Chats", models: "Models", backend: "Backend", memory: "Memory · soul.md", doctor: "Doctor", settings: "Settings", help: "Help & keys", providers: "Providers" };
  $("#view-title").textContent = titles[v] || v;
  if (v === "chats") renderChats();
  if (v === "models") renderModels();
  if (v === "market") renderMarket();
  if (v === "providers") renderProviders();
  if (v === "backend") renderBackends();
  if (v === "memory") renderSoul();
  if (v === "doctor") renderDoctor();
  if (v === "settings") renderSettings();
  if (v === "help") renderHelp();
}
$$(".nav-item").forEach((b) => (b.onclick = () => switchView(b.dataset.view)));

/* ---------------------------------------------------------------- chats */
async function renderChats() {
  const q = $("#chat-search").value.trim();
  const pinned = $("#pinned-only").checked;
  const data = await get(`/api/chats?q=${encodeURIComponent(q)}`);
  let rows = data.chats || [];
  if (pinned) rows = rows.filter((r) => r.pinned);
  $("#chats-count").textContent = rows.length || "";
  const grid = $("#chats-grid");
  grid.innerHTML = "";
  if (!rows.length) { grid.innerHTML = `<p class="muted" style="grid-column:1/-1">No chats yet.</p>`; return; }
  rows.sort((a, b) => (b.pinned - a.pinned) || (b.updated - a.updated));
  for (const r of rows) {
    const card = document.createElement("article");
    card.className = `chat-card${r.id === data.active ? " active" : ""}`;
    card.tabIndex = 0;
    card.innerHTML = `
      <h4>${r.pinned ? '<span class="pin">◆</span>' : ""}${esc(r.title)}</h4>
      <div class="meta">${esc(r.backend || "—")} · ${esc(r.model || "default")} · ${r.messages} msg · ${fmtDate(r.updated)}${r.tags?.length ? ` · ${r.tags.map(esc).join(" ")}` : ""}</div>
      <p class="prev">${esc(r.preview || "")}</p>
      <div class="acts">
        <button class="iconbtn pin" title="${r.pinned ? "Unpin this chat" : "Pin this chat to the top"}">${r.pinned ? "◆ pinned" : "◇ pin"}</button>
        <button class="iconbtn exp" title="Export this chat to markdown (shift/ctrl-click for JSON)">↧ export</button>
        <button class="iconbtn ren" title="Rename this chat">✎ rename</button>
        <button class="iconbtn del" title="Move this chat to the trash (an export is written first)">✕ delete</button>
      </div>`;
    card.onclick = async (e) => {
      const btn = e.target.closest(".iconbtn");
      if (!btn) { await openChat(r.id); return; }
      e.stopPropagation();
      if (btn.classList.contains("pin")) { await patch(`/api/chats/${r.id}`, { pinned: !r.pinned }); renderChats(); }
      if (btn.classList.contains("exp")) { const res = await post(`/api/chats/${r.id}/export`, { format: "markdown" }); toast(`exported → ${res.path}`); }
      if (btn.classList.contains("ren")) {
        const t = await promptModal("rename chat", "New title:", false, r.title);
        if (t) { await patch(`/api/chats/${r.id}`, { title: t }); renderChats(); }
      }
      if (btn.classList.contains("del")) {
        if (await confirmModal("move to trash", `Move "${r.title}" to trash? (exports a backup first)`, "Move to trash")) { await del(`/api/chats/${r.id}`); renderChats(); toast("moved to trash"); }
      }
    };
    card.onkeydown = (e) => { if (e.key === "Enter") openChat(r.id); };
    grid.appendChild(card);
  }
}
async function openChat(sid) {
  await loadSessionIntoView(sid);
  switchView("chat");
  refreshStatus();
}
$("#chat-search").oninput = debounce(renderChats, 250);
$("#pinned-only").onchange = renderChats;
$("#chats-refresh").onclick = renderChats;
function debounce(f, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => f(...a), ms); }; }
function fmtDate(ts) { const d = new Date(typeof ts === "number" ? ts * 1000 : ts); return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); }

/* --------------------------------------------------------------- models */
async function renderModels() {
  const sel = $("#model-backend");
  if (!sel.options.length) {
    const opts = ["all", "ollama", "lmstudio"];
    try {
      const pv = await get("/api/providers");
      if ((pv.providers || []).some((x) => x.has_key)) opts.push("cloud");
    } catch { /* providers unavailable: local-only listing */ }
    sel.innerHTML = opts.map((b) => `<option value="${b}"${b === "all" ? " selected" : ""}>${b === "all" ? "all backends" : b}</option>`).join("");
  }
  const want = sel.value || "all";
  const beList = want === "all"
    ? ["ollama", "lmstudio"].concat(sel.querySelector('option[value="cloud"]') ? ["cloud"] : [])
    : [want];
  const results = await Promise.all(beList.map(async (b) => {
    try { return [b, await get(`/api/models?backend=${b}`)]; }
    catch (e) { return [b, { error: String(e.message || e), models: [] }]; }
  }));
  const tb = $("#models-table tbody");
  tb.innerHTML = "";
  const q = ($("#model-search").value || "").toLowerCase();
  const sort = ($("#model-sort") || {}).value || "name";
  const QBITS = (qt) => { qt = (qt || "").toUpperCase(); if (qt.includes("IQ1")) return 1.2; if (qt.includes("IQ2")) return 2.2; if (qt.includes("IQ3")) return 3.3; if (qt.includes("IQ4") || qt.includes("MXFP4")) return 4.3; const m = qt.match(/Q(\d)/); const b = m ? +m[1] : (qt.includes("F16") || qt.includes("BF16") ? 16 : 8); return b + (qt.includes("XL") || qt.includes("_L") ? 0.4 : qt.includes("_S") ? -0.3 : 0); };
  const TH = { levels: 0, forced: 1, "on-off": 2, none: 3 };
  const FAM = (n) => (n.toLowerCase().split(/[/_ .-]+/)[0] || n);
  const sortRows = (rows) => {
    if (sort === "size") rows.sort((a, b) => (b.size_bytes || 0) - (a.size_bytes || 0));
    else if (sort === "params") rows.sort((a, b) => (b.params_b || 0) - (a.params_b || 0));
    else if (sort === "quant") rows.sort((a, b) => QBITS(a.quant) - QBITS(b.quant));
    else if (sort === "family") rows.sort((a, b) => FAM(a.name).localeCompare(FAM(b.name)) || a.name.localeCompare(b.name));
    else if (sort === "thinking") rows.sort((a, b) => (TH[a.thinking] ?? 4) - (TH[b.thinking] ?? 4));
    else if (sort === "vision") rows.sort((a, b) => (b.vision === true) - (a.vision === true) || (a.vision === null) - (b.vision === null));
    else rows.sort((a, b) => a.name.localeCompare(b.name));
    return rows;
  };
  let total = 0; const errs = [];
  for (const [b, data] of results) {
    const rows = sortRows((data.models || []).filter((m) => m.name.toLowerCase().includes(q)));
    if (data.error) errs.push(`${b}: ${data.error}`);
    if (!rows.length) continue;
    const sec = document.createElement("tr");
    sec.className = "msection";
    sec.innerHTML = `<td colspan="10">── ${b} ── <span class="muted">${data.error ? esc(data.error).slice(0, 80) : rows.length + " model(s)"}</span></td>`;
    tb.appendChild(sec);
    for (const m of rows) {
      total++;
      const tr = document.createElement("tr");
      if (m.name === data.default) tr.className = "active";
      const tools = m.tool_capable ? '<span class="fit-ok">yes</span>' : `<span class="fit-tight" title="${esc(m.why_weak || "")}">weak</span>`;
      tr.innerHTML = `
        <td style="white-space:normal">${m.loaded ? '<span class="pulse on"></span> ' : ""}<b>${esc(m.name)}</b>${m.warnings?.length ? `<br><small class="muted">${m.warnings.map(esc).join("; ")}</small>` : ""}</td>
        <td>${m.size || "—"}${m.quant ? ` <span class="muted">· ${esc(m.quant)}</span>` : ""}</td>
        <td>${m.params_b ? m.params_b + "B" : "—"}</td>
        <td>${m.ctx_len || "—"}</td>
        <td>${m.vram_min ? gib(m.vram_min) : "—"}</td>
        <td class="fit-${{ comfortable: "ok", tight: "tight", too_large: "over" }[m.fit] || ""}">${{ comfortable: "comfortable", tight: "tight", too_large: "too large" }[m.fit] || "—"}</td>
        <td>${tools}</td>
        <td>${m.vision === true ? '<span class="fit-ok">👁 yes</span>' : m.vision === false ? '<span class="muted">no</span>' : `<button class="btn small ghost vision-test" title="Send two random numbers as images and check the model reads them (loads the model — up to a minute)">test vision</button>`}</td>
        <td>${{ levels: '<span class="fit-ok">⚡ levels</span>', forced: '<span class="fit-ok">★ forced</span>', "on-off": '<span class="fit-tight">◐ on/off</span>', none: '<span class="muted">✗ none</span>' }[m.thinking] || '<span class="muted">?</span>'}</td>
        <td><button class="btn small use">use</button></td>`;
      const vt = tr.querySelector(".vision-test");
      if (vt) vt.onclick = async (ev) => {
        ev.stopPropagation();
        vt.disabled = true; vt.textContent = "probing…";
        try {
          const r = await post("/api/vision/probe", { backend: b, model: m.name });
          toast(`${m.name}: ${r.vision ? "CAN see images" : "cannot see images"} — cached`, r.vision ? "" : "bad");
        } catch (e) { toast(`vision probe failed: ${e.message}`, "bad"); }
        renderModels();
      };
      tr.querySelector(".use").onclick = async () => {
        await post("/api/model", { backend: b, model: m.name });
        toast(`${m.name} → default`); renderModels(); refreshStatus();
      };
      tr.ondblclick = () => tr.querySelector(".use").click();
      tb.appendChild(tr);
    }
  }
  $("#model-detail").textContent = errs.length
    ? `${total} model(s) · ${errs.join(" · ")}`
    : `${total} model(s)`;
}

const gib = (b) => (b / 2 ** 30).toFixed(1) + " GiB";

// -------------------------------------------------------------- providers
let _provData = null;

async function renderProviders() {
  const box = $("#prov-list");
  if (!_provData) {
    try { _provData = await get("/api/providers"); }
    catch { box.innerHTML = '<p class="prov-sub">providers unavailable</p>'; return; }
  }
  const q = ($("#prov-search").value || "").toLowerCase();
  const rows = _provData.providers.filter((p) =>
    !q || p.name.toLowerCase().includes(q) || p.blurb.toLowerCase().includes(q) || p.pid.includes(q));
  const groups = [["Popular", rows.filter((p) => p.popular)],
                  ["Other", rows.filter((p) => !p.popular && p.pid.indexOf("custom-") !== 0)],
                  ["Custom", rows.filter((p) => p.pid.indexOf("custom-") === 0)]];
  box.innerHTML = groups.filter(([, list]) => list.length).map(([g, list]) =>
    `<div class="prov-group">${g}</div>` + list.map((p) => {
      const ic = PROV_ICONS[p.pid];
      const glyph = ic ? `<svg viewBox="0 0 24 24" aria-hidden="true" fill="${_logoColor(ic[0])}"><path d="${ic[1]}"/></svg>`
        : esc(p.name.slice(0, 1).toUpperCase());
      const badges = [
        p.active ? '<span class="pb active">active</span>' : "",
        p.recommended ? '<span class="pb rec">Recommended</span>' : "",
        p.has_key ? `<span class="pb ok">✓ ${esc(p.masked)}</span>` : "",
      ].join("");
      const blurb = p.models ? `${p.blurb} · ${p.models}` : p.blurb;
      return `<div class="prov-row" data-pid="${esc(p.pid)}" tabindex="0" role="button" aria-label="Connect ${esc(p.name)}">
        <span class="prov-glyph${ic ? " has-logo" : ""}">${glyph}</span>
        <span class="prov-name">${esc(p.name)}</span>
        <span class="prov-blurb">${esc(blurb)}</span>${badges}
        <span class="prov-actions"></span>
      </div>`;
    }).join("")).join("")
    + `<div class="prov-foot"><button type="button" class="btn ghost" id="prov-add-custom">✦ Custom OpenAI-compatible provider</button></div>`;
  $$(".prov-row").forEach((r) => {
    const go = () => provConnect(r.dataset.pid);
    r.onclick = go;
    r.onkeydown = (ev) => { if (ev.key === "Enter") go(); };
  });
  $("#prov-add-custom").onclick = provAddCustom;
}

async function provConnect(pid) {
  const p = (_provData.providers || []).find((x) => x.pid === pid);
  if (!p) return;
  const cur = p.has_key ? `Stored key: ${p.masked} — paste to replace, empty to remove.` : "";
  const signup = p.signup_url ? ` Get one: <a href="${esc(p.signup_url)}" target="_blank" rel="noopener">${esc(p.signup_url)}</a>` : "";
  const key = await promptModal(`🔑 ${p.name} API key`, cur + (cur ? "\n" : "") + signup, true);
  if (key === null) return;
  let models = p.models || "";
  const r = await post("/api/providers/key", { pid, key, models, probe: true });
  if (r && r.error) { toast(r.error, "bad"); return; }
  _provData = r; renderProviders();
  toast(key ? `${p.name} key saved` : `${p.name} key removed`);
  if (key && r.models_ok === false) {
    // gateway has no /models listing — only now ask for ids
    const m = await promptModal(`${p.name} model ids`,
      "this gateway has no /models listing — comma-separated ids, e.g. qwen3-coder, llama-3.3-70b. Append :128k or :1m to a model to declare its context window.");
    if (m) {
      const r2 = await post("/api/providers/key", { pid, key, models: m });
      if (r2 && !r2.error) { _provData = r2; renderProviders(); toast(`${p.name}: ${m.split(",").length} model(s) registered`); }
    }
  }
  if (key) {
    const u = await post("/api/providers/use", { pid });
    if (u && !u.error) { _provData = u; renderProviders(); toast(`${p.name} → default backend (cloud)`); }
  }
}

async function provAddCustom() {
  const name = await promptModal("Custom provider — name", "e.g. Home vLLM");
  if (!name) return;
  const base = await promptModal("Custom provider — base URL", "https://host:port/v1 (OpenAI-compatible)");
  if (!base) return;
  const models = await promptModal("Model ids (optional)", "comma-separated, e.g. qwen3-coder, llama-3.3-70b — leave empty to list from /models");
  if (models === null) return;
  const r = await post("/api/providers/custom", { name, base_url: base, models: models || "" });
  if (r && r.error) { toast(r.error, "bad"); return; }
  _provData = r; renderProviders(); toast(`${name} added — click it to paste the key`);
}

$("#prov-search")?.addEventListener("input", () => renderProviders());

// ------------------------------------------------------------- marketplace
let _marketTimer = null;

async function renderMarket() {
  const grid = $("#market-grid");
  const head = $("#market-head");
  if (grid.dataset.loaded) { _paintMarket(_marketData); _pollMarket(); return; }
  head.textContent = "scanning your hardware…";
  let data = null;
  try { data = await get("/api/market"); } catch { head.textContent = "market unavailable"; return; }
  grid.dataset.loaded = "1";
  _marketData = data;
  _paintMarket(data);
}
let _marketData = null;
function _paintMarket(data) {
  const grid = $("#market-grid");
  const head = $("#market-head");
  const g = data.vram_bytes ? Math.round(data.vram_bytes / 1073741824) : null;
  const badge = { fits: ["✓", "ok"], tight: ["⚠", "warn"], no: ["✗", "bad"], unknown: ["?", "dim"] };
  const entries = data.entries || [];
  const SECTIONS = [["llm", "💬 Models"], ["stt", "🎤 Speech-to-text"]];
  const active = grid.dataset.section || "llm";
  const tabs = SECTIONS.map(([k, label]) => {
    const n = entries.filter((e) => (e.section || "llm") === k).length;
    return `<button class="chip ${k === active ? "active" : ""}" data-sec="${k}">${label}<span class="cnt">${n}</span></button>`;
  }).join(" ");
  head.innerHTML = `<span class="market-gpu">${data.gpu ? esc(data.gpu) + " · " : ""}${g ? g + " GB VRAM" : "VRAM unknown — fit not judged"}</span><span class="market-seg">${tabs}</span>`;
  $$("#market-head .chip").forEach((c) => (c.onclick = () => { grid.dataset.section = c.dataset.sec; _paintMarket(data); }));
  const vramGb = data.vram_bytes ? data.vram_bytes / 1073741824 : null;
  grid.innerHTML = entries.filter((e) => (e.section || "llm") === active).map((e) => {
    const sec = e.section || "llm";
    const [txt, cls] = badge[e.fit] || badge.unknown;
    const size = sec === "llm" ? e.size_gb.toFixed(1) + " GB" : (e.size_gb * 1000).toFixed(0) + " MB";
    const fit = sec === "llm"
      ? `<span class="mc-fit ${cls}">${txt} ${esc(e.fit_label)}${e.kind !== "gguf" ? " · " + esc(e.kind) : ""}</span>`
      : `<span class="mc-fit dim">🎤 speech-to-text · ~/.ohmyllm/voice/</span>`;
    let bar = "";
    if (sec === "llm" && vramGb) {
      const pct = Math.min(100, Math.round((e.size_gb / vramGb) * 100));
      bar = `<div class="mc-bar" title="${e.size_gb} GB of ${vramGb.toFixed(0)} GB VRAM"><i class="${cls}" style="width:${pct}%"></i></div>`;
    }
    return `<div class="market-card f-${cls}" data-mid="${esc(e.mid)}" tabindex="0" role="button" aria-label="Download ${esc(e.title)}">
      <div class="mc-top"><span class="mc-title">${esc(e.title)}</span><span class="mc-size">${size}</span></div>
      ${fit}
      <div class="mc-blurb">${esc(e.blurb)}</div>
      ${bar}
      <div class="mc-foot"><span class="mc-repo">${esc(e.repo)}</span><span class="mc-dl">↓ Download</span></div>
    </div>`;
  }).join("");
  $$(".market-card").forEach((c) => {
    const go = () => marketDownload(c.dataset.mid);
    c.onclick = go;
    c.onkeydown = (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); } };
  });
  _pollMarket();
}


async function marketDownload(mid) {
  let r = null;
  try { r = await post("/api/market/download", { mid }); }
  catch (e) { toast(String(e.message || e), "bad"); return; }
  $("#market-dl").hidden = false;
  toast(r.started ? `downloading ${r.started}…` : (r.error || ""), r.error ? "bad" : "");
  _pollMarket();
}

async function _pollMarket() {
  if (_marketTimer) return;
  _marketTimer = setInterval(async () => {
    let p = null;
    try { p = await get("/api/market/progress"); } catch { return; }
    if (!p || (p.active === false && !p.total)) return;
    const box = $("#market-dl");
    if (!p.active && !p.done && !p.error) { box.hidden = true; return; }
    box.hidden = false;
    const pct = p.total ? Math.min(100, (p.received / p.total) * 100) : 0;
    $("#market-bar-fill").style.width = pct + "%";
    const gb = (n) => (n / 1e9).toFixed(2);
    $("#market-dl-label").textContent = p.error
      ? `${p.title}: ${p.error}`
      : p.done ? `✓ ${p.title} ready — reload Models and load it`
      : `${p.title}  ${gb(p.received)}/${gb(p.total)} GB · ${p.mbps.toFixed(1)} MB/s · ${p.eta_s}s left`;
    $("#market-dl-cancel").style.visibility = (p.done || p.error) ? "hidden" : "visible";
    if (!p.active) {
      clearInterval(_marketTimer); _marketTimer = null;
      if (p.done || p.error) setTimeout(() => { $("#market-dl").hidden = true; }, 4000);
    }
  }, 600);
}

$("#market-dl-cancel").onclick = () => post("/api/market/cancel", {});

$("#models-refresh").onclick = renderModels;
$("#model-sort").onchange = renderModels;
$("#model-backend").onchange = renderModels;
$("#model-search").oninput = debounce(renderModels, 200);
$("#models-pull").onclick = async () => {
  const name = await promptModal("pull model", "Ollama model tag to pull (e.g. qwen2.5:7b-instruct):");
  if (!name) return;
  const bar = $("#pull-bar"); bar.hidden = false; bar.value = 0;
  try {
    const res = await fetch(`${BASE}/api/pull`, { method: "POST", headers: { "Content-Type": "application/json", "X-OMLLM-Token": BOOT.token }, body: JSON.stringify({ backend: "ollama", name }) });
    const r = res.body.getReader(), d = new TextDecoder(); let buf = "";
    while (true) {
      const { done, value } = await r.read(); if (done) break;
      buf += d.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const f = buf.slice(0, i); buf = buf.slice(i + 2);
        const dl = (f.match(/^data: (.*)$/m) || [])[1] || "{}";
        const j = JSON.parse(dl);
        if (f.startsWith("event: progress")) { bar.value = (j.pct || 0) * 100; }
        if (f.startsWith("event: done")) { bar.hidden = true; j.ok ? toast(`pulled ${name}`) : toast(`pull failed: ${j.error}`, "err"); renderModels(); }
      }
    }
  } catch (e) { bar.hidden = true; toast(`pull failed: ${e.message}`, "err"); }
};

/* -------------------------------------------------------------- backend */
async function renderBackends() {
  const rows = await get("/api/backends");
  const cards = $("#backend-cards");
  cards.innerHTML = "";
  const cur = state.config?.default_backend;
  const meta = {
    ollama: { t: "Ollama", d: "Local llama.cpp server with first-class tool calling.", url: state.config?.ollama?.host },
    lmstudio: { t: "LM Studio", d: "Desktop app exposing an OpenAI-compatible API.", url: state.config?.lmstudio?.base_url },
    cloud: { t: "☁ Cloud", d: "Hosted OpenAI-compatible providers (Alibaba, DeepSeek, OpenRouter, …) via a connected API key.", url: null },
    mock: { t: "Mock backend", d: "Scripted agent for demos and tests — no model required.", url: null },
  };
  const keys = Object.keys(meta).filter((k) => k !== "mock" || cur === "mock");
  for (const k of keys) {
    const r = rows[k] || {};
    const el = document.createElement("div");
    el.className = `be-card-ui${k === cur ? " sel" : ""}`;
    el.innerHTML = `
      <h3><span class="pulse ${r.running ? "on" : k === "mock" ? "on" : "off"}"></span>${meta[k].t}</h3>
      <p class="be-row">${meta[k].d}</p>
      <p class="be-row mono small">${r.running ? (r.health ? "running · " + esc(r.health) : "running") : r.installed ? "installed · not running" : "not detected"}${meta[k].url ? ` · ${esc(meta[k].url)}` : ""}</p>
      <div class="be-acts">
        <button class="btn small use">default</button>
        ${["mock", "cloud"].includes(k) ? "" : '<button class="btn small start">start</button>'}
      </div>`;
    el.querySelector(".use").onclick = async () => { await post("/api/backend", { name: k }); toast(`${meta[k].t} is now the default`); refreshStatus(); renderBackends(); };
    const st = el.querySelector(".start");
    if (st) st.onclick = async () => {
      st.disabled = true; st.textContent = "starting…";
      const res = await post("/api/backend", { name: k, start: true });
      toast(res.message || (res.ok ? "started" : "not started"), res.ok ? "" : "warn");
      if (!res.ok && res.instructions?.length) { const ins = $("#backend-instructions"); ins.hidden = false; ins.textContent = res.instructions.join("\n"); } else { $("#backend-instructions").hidden = true; }
      st.disabled = false; st.textContent = "start";
      renderBackends();
    };
    cards.appendChild(el);
  }
}

/* ---------------------------------------------------------------- soul */
async function renderSoul() {
  const d = await get("/api/soul");
  $("#soul-editor").value = d.text;
  $("#soul-path").textContent = d.path;
  const w = $("#mem-writes"); w.checked = !!(state.config?.memory?.agent_writes);
  w.onchange = async () => { await saveConfig({ memory: { ...state.config.memory, agent_writes: w.checked } }); };
  $("#soul-recent").innerHTML = (d.recent || []).map((r) => `<div><b>${fmtDate(r.ts)}</b> ${esc(r.action)} ${esc((r.text || "").slice(0, 70))}</div>`).join("");
}
$("#soul-save").onclick = async () => { await post("/api/soul", { action: "write", text: $("#soul-editor").value }); toast("soul.md saved (backup kept)"); renderSoul(); };
$("#soul-revert").onclick = async () => { await post("/api/soul", { action: "revert" }); toast("reverted to latest backup"); renderSoul(); };
$("#soul-clear").onclick = async () => { if (await confirmModal("clear soul", "Clear soul.md? (a backup is kept first)", "Clear")) { await post("/api/soul", { action: "clear" }); renderSoul(); toast("cleared"); } };

/* -------------------------------------------------------------- doctor */
async function renderDoctor() {
  const list = $("#doctor-list");
  list.innerHTML = `<li class="muted">running checks…</li>`;
  const d = await get("/api/doctor");
  list.innerHTML = "";
  const icon = { pass: "PASS", warn: "WARN", fail: "FAIL", skip: "SKIP" };
  for (const c of d.checks) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="st ${c.status}">${icon[c.status] || c.status}</span><span>${esc(c.label)}<br><small class="muted">${esc(c.detail)}</small></span>${c.fix ? `<span class="hint">${esc(c.fix)}</span>` : ""}`;
    list.appendChild(li);
  }
  const sm = d.summary || {}; list.insertAdjacentHTML("beforeend", `<li class="muted">${sm.pass||0} pass · ${sm.warn||0} warn · ${sm.fail||0} fail</li>`);
}
$("#doctor-run").onclick = renderDoctor;

/* ------------------------------------------------------------ settings */
const SCHEMA = [
  ["§", "general"],
  ["default_backend", "backend", "select", ["ollama", "lmstudio", "cloud", "mock"]],
  ["§", "ollama"],
  ["ollama.host", "host", "text"], ["ollama.binary", "binary name", "text"],
  ["ollama.auto_start", "auto-start server", "bool"], ["ollama.startup_timeout", "startup timeout s", "num"],
  ["§", "lm studio"],
  ["lmstudio.base_url", "base URL", "text"], ["lmstudio.auto_start", "auto-start", "bool"],
  ["lmstudio.binary_path", "app path (optional)", "text"],
  ["§", "generation"],
  ["generation.temperature", "temperature", "num"], ["generation.top_p", "top_p", "num"],
  ["generation.top_k", "top_k", "num"], ["generation.repeat_penalty", "repeat penalty", "num"],
  ["generation.num_ctx", "context window", "num"], ["generation.num_gpu", "gpu layers (-1=all)", "num"],
  ["generation.num_batch", "batch size", "num"], ["generation.max_tokens", "max tokens", "num"],
  ["generation.kv_cache", "K/V cache quant", "select", ["f16", "q8_0", "q4_0", "q4_1", "iq4_nl", "fp8", "auto"]],
  ["generation.effort", "reasoning effort", "select", ["off", "low", "medium", "high", "xhigh", "max"]],
  ["§", "agent"],
  ["agent.max_steps", "max steps per turn", "num"], ["agent.max_parse_retries", "parse retries", "num"],
  ["agent.tool_timeout", "tool timeout s", "num"],
  ["agent.native_tools", "native tools", "select", ["auto", "force", "off"]],
  ["agent.react_fallback", "ReAct fallback", "bool"],
  ["agent.thinking_visible", "stream live thinking", "bool"],
  ["agent.agent_loop", "relentless agent loop", "bool"],
  ["agent.tone", "tone of voice", "select", ["default", "ruthless", "chill", "friendly", "professional", "terse"]],
  ["§", "memory"],
  ["memory.enabled", "soul memory enabled", "bool"], ["memory.agent_writes", "agent may write memories", "bool"],
  ["memory.require_explicit_save", "require explicit save intent", "bool"],
  ["memory.max_inject_chars", "inject budget chars", "num"], ["memory.backups_keep", "backups kept", "num"],
  ["§", "safety"],
  ["safety.command_allowlist_extra", "extra allowlisted commands (comma)", "list"],
  ["safety.command_denylist", "hard-denied commands (comma)", "list"],
  ["safety.allow_sensitive_paths", "sensitive-path re-allow globs (comma)", "list"],
  ["safety.workspace_write_requires_confirmation", "confirm workspace writes", "bool"],
  ["safety.catastrophic_override", "override catastrophic block (dangerous!)", "bool"],
  ["safety.http_fetch_enabled", "network tool enabled", "bool"],
  ["§", "mail (see: oml mail setup)"],
  ["mail.enabled", "mail tools enabled", "bool"],
  ["mail.address", "account address", "text"],
  ["mail.imap_host", "IMAP host", "text"],
  ["mail.smtp_host", "SMTP host", "text"],
  ["mail.signature", "signature appended to outgoing mail", "text"],
  ["mail.recipient_allowlist", "send allowlist (comma; empty = unrestricted)", "text"],
  ["§", "danger guard (clef-flash, cpu)"],
  ["guard.enabled", "flag dangerous calls in dangerous/full-access modes", "bool"],
  ["guard.base_url", "decision server URL", "text"],
  ["guard.model", "decision model", "text"],
  ["guard.danger_threshold", "danger threshold (0-1)", "num"],
  ["guard.timeout_s", "guard timeout s", "num"],
];
async function renderSettings() {
  if (!state.config) state.config = await get("/api/config");
  const g = $("#settings-form");
  g.innerHTML = "";
  const gv = (p) => p.split(".").reduce((o, k) => (o || {})[k], state.config);
  const sv = (p, v) => { const ks = p.split("."); let o = state._draft; ks.forEach((k, i) => { if (i === ks.length - 1) o[k] = v; else o = o[k] = { ...(o[k] || {}) }; }); };
  state._draft = JSON.parse(JSON.stringify(state.config));
  for (const row of SCHEMA) {
    if (row[0] === "§") { g.insertAdjacentHTML("beforeend", `<div class="set-row section"><label>${row[1]}</label></div>`); continue; }
    const [path, label, kind, min, max] = row;
    const val = gv(path);
    let input;
    if (kind === "bool") input = `<input type="checkbox" data-p="${path}" ${val ? "checked" : ""}>`;
    else if (kind === "select") input = `<select data-p="${path}">${min.map((o) => `<option ${o === val ? "selected" : ""}>${o}</option>`).join("")}</select>`;
    else if (kind === "num") input = `<input type="number" data-p="${path}" value="${val ?? 0}" ${row[3] != null ? `min=${row[3]}` : ""} ${row[4] != null ? `max=${row[4]}` : ""} step="any">`;
    else if (kind === "list") input = `<input type="text" class="wide" data-p="${path}" value="${esc((val || []).join(", "))}">`;
    else input = `<input type="text" class="wide" data-p="${path}" value="${esc(val ?? "")}">`;
    g.insertAdjacentHTML("beforeend", `<div class="set-row"><label>${label}</label>${input.replace("data-p=", 'data-k="${kind}" data-p=')}</div>`);
  }
  g.querySelectorAll("[data-p]").forEach((el) => {
    el.onchange = () => {
      let v = el.type === "checkbox" ? el.checked : el.type === "number" ? parseFloat(el.value) : el.value;
      if (el.dataset.k === "list") v = String(v).split(",").map((x) => x.trim()).filter(Boolean);
      sv(el.dataset.p, v);
    };
  });
}
$("#settings-save").onclick = async () => {
  const merged = state._draft;
  const res = await api("/api/config", { method: "PATCH", body: JSON.stringify(merged) });
  state.config = res.config; toast("settings saved"); refreshStatus();
};
async function saveConfig(partial) {
  const merged = JSON.parse(JSON.stringify(state.config || (await get("/api/config"))));
  Object.assign(merged, partial);
  const res = await api("/api/config", { method: "PATCH", body: JSON.stringify(merged) });
  state.config = res.config; refreshStatus();
}

/* ---------------------------------------------------------------- help */
function renderHelp() {
  $("#help-body").innerHTML = `
  <h3>keys</h3>
  <table><tr><td><kbd>Ctrl K</kbd></td><td>command palette</td></tr>
  <tr><td><kbd>Ctrl N</kbd></td><td>new chat</td></tr><tr><td><kbd>Ctrl O</kbd></td><td>chats</td></tr>
  <tr><td><kbd>Ctrl M</kbd></td><td>models</td></tr><tr><td><kbd>Ctrl B</kbd></td><td>backend</td></tr>
  <tr><td><kbd>Ctrl S</kbd></td><td>memory</td></tr><tr><td><kbd>Ctrl D</kbd></td><td>doctor</td></tr>
  <tr><td><kbd>Ctrl G</kbd></td><td>settings</td></tr><tr><td><kbd>Ctrl P</kbd></td><td>permission mode</td></tr>
  <tr><td><kbd>Esc</kbd></td><td>close modal / stop</td></tr><tr><td><kbd>Enter</kbd></td><td>send · <kbd>Shift Enter</kbd> newline</td></tr></table>
  <h3>slash commands</h3>
  <table>
  <tr><td><code>/remember &lt;fact&gt;</code></td><td>write a durable memory to soul.md</td></tr>
  <tr><td><code>/ultrathink [on|off]</code></td><td>deep reasoning: the agent thinks much longer and harder</td></tr>
  <tr><td><code>/agentloop [on|off]</code></td><td>relentless loop: never stop until the result is verified perfect</td></tr>
  <tr><td><code>/new</code></td><td>start a fresh session</td></tr>
  <tr><td><code>/rename &lt;title&gt;</code></td><td>rename the current chat</td></tr>
  <tr><td><code>/export [md|json]</code></td><td>export the current chat</td></tr>
  <tr><td><code>/models</code></td><td>open the model list</td></tr>
  <tr><td><code>/mode &lt;name&gt;</code></td><td>request a permission mode (full_access needs the typed phrase)</td></tr>
  <tr><td><code>/clear</code></td><td>empty the visible stream</td></tr>
  <tr><td><code>/market</code></td><td>models + voice models to download (Market tab)</td></tr>
  <tr><td><code>/providers</code></td><td>connect cloud API keys — Providers tab</td></tr>
  <tr><td><code>/voice</code></td><td>speech-to-text status · hold <kbd>Ctrl+E</kbd> anywhere to talk</td></tr>
  <tr><td><code>/mail</code></td><td>email account status · <code>oml mail setup</code> to connect</td></tr>
  <tr><td><code>/tone</code></td><td>how the AI talks: default · ruthless · chill · friendly · professional · terse</td></tr>
  <tr><td><code>/help</code></td><td>this screen</td></tr></table>
  <h3>where do API keys go?</h3>
  <p>Open the <b>Providers</b> tab (left sidebar) → pick OpenRouter / Anthropic / OpenAI / … →
  paste the key. It is stored in <code>~/.ohmyllm/config.toml</code> (owner-only, chmod 600)
  and only ever shown masked. Environment keys (<code>OMLLM_KEY_OPENROUTER</code>,
  <code>OPENAI_API_KEY</code>, …) also work and take precedence over the file.</p>
  <h3>permission modes</h3>
  <table>
  <tr><td><b>strict</b></td><td>read-only tools auto; every write/exec asks approval</td></tr>
  <tr><td><b>safe</b> (default)</td><td>reads auto; writes/execs within risk bounds ask; network gated</td></tr>
  <tr><td><b>sandboxed</b></td><td>exec limited to an allowlist; writes stay in the workspace</td></tr>
  <tr><td><b>dangerous</b></td><td>most approvals skipped; destructive ops still gated</td></tr>
  <tr><td><b>full_access</b></td><td>no approvals. Requires typing <code>I ACCEPT FULL ACCESS</code>. Catastrophic commands stay blocked.</td></tr></table>
  <h3>memory</h3>
  <p><code>soul.md</code> is plain Markdown the agent reads every session and may update (if allowed). Backups are kept automatically; use the Memory screen to edit, revert, or clear.</p>
  <h3>privacy</h3>
  <p>OhMyLLM talks only to local model servers (Ollama / LM Studio / mock) over loopback. Nothing leaves this machine unless you enable the gated <code>http_fetch</code> tool yourself. The GUI bridge binds 127.0.0.1 only and requires a per-process token.</p>`;
}

/* ------------------------------------------------------------- composer */
const SLASHES = [
  ["/remember", "write a memory"], ["/new", "fresh chat"], ["/rename", "rename chat"], ["/market", "model marketplace"],
  ["/export", "export chat"], ["/models", "model list"], ["/mode", "permission mode"],
  ["/backend", "backend screen"], ["/clear", "clear stream"], ["/help", "help"], ["/mail", "email status"], ["/voice", "voice status"], ["/tone", "how the AI talks"],
  ["/ultrathink", "deep reasoning on/off"], ["/agentloop", "relentless verified-completion loop"], ["/think", "off|low|medium|high|xhigh|max"], ["/skill:", "cast a skill: /skill:<name>"],
];
$("#input").addEventListener("input", () => {
  autoGrow();
  const v = $("#input").value;
  if (v.startsWith("/") && !v.includes("\n")) {
    const m = v.slice(1).split(" ")[0].toLowerCase();
    const hits = SLASHES.filter((s) => s[0].slice(1).startsWith(m));
    const hint = $("#slash-hint");
    if (hits.length && hits.length < 9 && m) {
      hint.hidden = false;
      hint.innerHTML = hits.map(([c, d], i) => `<div class="${i === 0 ? "sel" : ""}" data-c="${c}"><b>${c}</b><span class="muted">${d}</span></div>`).join("");
      hint.querySelectorAll("div").forEach((dv) => (dv.onclick = () => { $("#input").value = dv.dataset.c + " "; $("#input").focus(); hint.hidden = true; }));
    } else hint.hidden = true;
  } else $("#slash-hint").hidden = true;
});
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("#slash-hint").hidden = true; if (state.streaming) post("/api/cancel", {}); }
  if ($("#slash-hint").hidden !== true && (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey))) {
    const sel = $("#slash-hint .sel");
    if (sel) { e.preventDefault(); sel.click(); return; }
  }
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    const opts = $$("#slash-hint div");
    if (opts.length) {
      e.preventDefault();
      const i = opts.findIndex((o) => o.classList.contains("sel"));
      opts[i]?.classList.remove("sel");
      const j = (i + (e.key === "ArrowDown" ? 1 : opts.length - 1)) % opts.length;
      opts[j].classList.add("sel");
    }
  }
});
function autoGrow() { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 240) + "px"; }

$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#input").value.trim();
  if (!text || state.streaming) return;
  $("#input").value = ""; autoGrow(); $("#slash-hint").hidden = true;
  if (text.startsWith("/") && !text.startsWith("/remember")) { handleSlash(text); return; }
  addMsg("user", text);
  scrollBottom(true);  // the user's own send always jumps to the new message
  if (text.startsWith("/remember")) $("#input").value = "";
  state.streaming = true;
  $("#send").disabled = true; $("#cancel").hidden = false;
  const mem = text.startsWith("/remember");
  const payload = mem ? text.slice("/remember".length).trim() || "important fact from user" : text;
  await sendToAgent(payload, mem);
  if (state.streaming) { state.streaming = false; $("#send").disabled = false; $("#cancel").hidden = true; }
});
$("#cancel").onclick = () => post("/api/cancel", {});

async function handleSlash(text) {
  const [cmd, ...rest] = text.split(" ");
  const arg = rest.join(" ");
  switch (cmd.toLowerCase()) {
    case "/new": newChat(); break;
    case "/clear": emptyState(); break;
    case "/help": switchView("help"); break;
    case "/models": switchView("models"); break;
    case "/market": switchView("market"); break;
    case "/providers": switchView("providers"); break;
    case "/onboarding": toast("the guided tutorial lives in the TUI — run: oml"); break;
    case "/voice": {
      const r = await get("/api/voice");
      addEvline("note", "voice · " + (r.line || "unavailable"));
      break;
    }
    case "/mail": {
      const c = await get("/api/config");
      const m = (c.mail || {});
      addEvline("note", m.address
        ? `mail · ${m.address} · ${m.enabled ? "ready (send asks first outside full_access)" : "disabled — enable in Settings"}`
        : "mail · no account — run `oml mail setup` in a terminal (drafts work without it)");
      break;
    }
    case "/backend": switchView("backend"); break;
    case "/rename": if (state.session) patch(`/api/chats/${state.session}`, { title: arg || "renamed" }).then(refreshStatus); break;
    case "/export": if (state.session) post(`/api/chats/${state.session}/export`, { format: arg.includes("json") ? "json" : "markdown" }).then((r) => toast(`exported → ${r.path}`)); break;
    case "/mode": setModeUi(arg); break;
    case "/think": {
      const levels = ["off", "low", "medium", "high", "xhigh", "max"];
      let want = (arg || "").toLowerCase();
      if (want === "on") want = "medium";
      if (!levels.includes(want)) {
        const cur = (state.config && state.config.generation && state.config.generation.effort) || "off";
        toast(`effort is ${cur} — /think ${levels.join("|")}`);
        break;
      }
      const merged = JSON.parse(JSON.stringify(state.config || (await get("/api/config"))));
      merged.generation.effort = want;
      merged.generation.thinking = want !== "off";
      const r = await api("/api/config", { method: "PATCH", body: JSON.stringify(merged) });
      if (r && r.error) { toast(`think: ${r.error}`); break; }
      state.config = r.config;
      toast(`reasoning effort → ${want}`);
      refreshStatus();
      break;
    }
    default: {
      if (cmd.toLowerCase().startsWith("/skill:")) {
        const name = cmd.slice(7);
        const r = await post("/api/skills/cast", { name, session: state.session });
        if (r && r.error) toast(r.error + (r.skills ? " · have: " + r.skills.slice(0, 6).join(", ") : ""));
        else { toast(`skill cast · ${r.cast}`); if (state.session) loadSessionIntoView(state.session); }
        break;
      }
      addEvline("note", `unknown command ${cmd}`);
    }
    case "/ultrathink": {
      const cur = !!(state.config && state.config.generation && state.config.generation.ultrathink);
      const want = arg === "on" ? true : arg === "off" ? false : !cur;
      const merged = JSON.parse(JSON.stringify(state.config || (await get("/api/config"))));
      merged.generation = merged.generation || {};
      merged.generation.ultrathink = want;
      const r = await api("/api/config", { method: "PATCH", body: JSON.stringify(merged) });
      if (r && r.error) { toast(`ultrathink: ${r.error}`); break; }
      state.config = r.config;
      toast(want ? "ultrathink ON — deep thinking from the next turn" : "ultrathink off");
      refreshStatus();
      break;
    }
    case "/agentloop": {
      const cur = !!(state.config && state.config.agent && state.config.agent.agent_loop);
      const want = arg === "on" ? true : arg === "off" ? false : !cur;
      const merged = JSON.parse(JSON.stringify(state.config || (await get("/api/config"))));
      merged.agent = merged.agent || {};
      merged.agent.agent_loop = want;
      const r = await api("/api/config", { method: "PATCH", body: JSON.stringify(merged) });
      if (r && r.error) { toast(`agentloop: ${r.error}`); break; }
      state.config = r.config;
      toast(want ? "🔁 agent loop ON — verified completion from the next turn" : "agent loop off");
      refreshStatus();
      break;
    }
    case "/tone": {
      const opts = ["default", "ruthless", "chill", "friendly", "professional", "terse"];
      const want = (arg || "").toLowerCase();
      if (!want) {
        const cur = (state.config && state.config.agent && state.config.agent.tone) || "default";
        addEvline("note", `tone · current: ${cur} — /tone ${opts.filter((o) => o !== cur).join("|")}`);
        break;
      }
      if (!opts.includes(want)) { toast(`unknown tone "${want}" — ${opts.join("|")}`, "bad"); break; }
      const merged = JSON.parse(JSON.stringify(state.config || (await get("/api/config"))));
      merged.agent = merged.agent || {};
      merged.agent.tone = want;
      const r = await api("/api/config", { method: "PATCH", body: JSON.stringify(merged) });
      if (r && r.error) { toast(`tone: ${r.error}`, "bad"); break; }
      state.config = r.config;
      toast(`tone → ${want} (from the next turn)`);
      refreshStatus();
      break;
    }
  }
}

async function newChat() {
  const s = await post("/api/chats", {});
  state.session = s.id;
  clearStream(); emptyState();
  $("#g-title").textContent = s.title;
  refreshStatus();
}
$("#btn-new").onclick = newChat;

/* ------------------------------------------------------------ mode chip */
async function setModeUi(preset) {
  const m = document.createElement("div");
  m.className = "modal"; m.id = "mode-modal";
  const modes = ["strict", "safe", "sandboxed", "dangerous", "full_access"];
  m.innerHTML = `<header><h3>permission mode</h3><span class="risk-badge ${state.status?.mode === "full_access" ? "exec" : "read"}">${esc(state.status?.mode || "")}</span></header>
  <div class="ap-body">${modes.map((x) => `<div class="opt" data-m="${x}" role="button" tabindex="0"><div><b>${x}</b><div class="d small muted">${MODE_DESC[x]}</div></div></div>`).join("")}
  ${preset && modes.includes(preset) ? "" : ""}</div>
  <footer><span class="muted small">The model can never change this; only you can. full_access requires a typed confirmation.</span><span class="spacer"></span><button class="btn" id="mode-close">Close</button></footer>`;
  document.body.appendChild(m);
  $("#scrim").hidden = false; m.hidden = false;
  m.querySelectorAll(".opt").forEach((o) => {
    o.onclick = async () => {
      const mode = o.dataset.m;
      if (mode === "full_access") {
        const ph = await promptModal("full access", 'Type the exact phrase shown in the sidebar ("I ACCEPT FULL ACCESS") to enable full access:');
        try { await post("/api/mode", { mode, phrase: ph || "" }); toast("full access enabled"); }
        catch (e) { toast(e.message, "err"); return; }
      } else {
        await post("/api/mode", { mode }); toast(`mode → ${mode}`);
      }
      m.remove(); $("#scrim").hidden = true; refreshStatus();
    };
    if (preset && o.dataset.m === preset) o.click();
  });
  document.getElementById("mode-close").onclick = () => { m.remove(); $("#scrim").hidden = true; };
}
$("#chip-mode").onclick = () => setModeUi();
const MODE_DESC = {
  strict: "Reads auto. Every write or command needs approval.",
  safe: "Reads auto; risky ops ask. Network gated. (recommended)",
  sandboxed: "Exec limited to an allowlist; writes stay in the workspace.",
  dangerous: "Approvals mostly skipped; catastrophic still blocked.",
  full_access: "No approvals at all. You own everything that happens.",
};

/* --------------------------------------------------------------- status */
async function refreshStatus() {
  try {
    const s = await get("/api/status");
    state.status = s;
    const dot = $("#backend-dot");
    dot.className = `pulse ${s.backend_ready ? "on" : "off"}`;
    $("#brand-sub").textContent = `${s.backend} · ${s.workspace.split("/").pop() || s.workspace}`;
    $("#version").textContent = `v${s.version} · soul: ${s.soul.split("/").pop()}`;
    $("#chip-model").textContent = (s.model || "no model") + (s.ultrathink ? " 🌀" : (s.effort && s.effort !== "off" ? " ⚡" + s.effort : ""));
    $("#chip-model").title = `model · ${s.backend}`;
    const mode = $("#chip-mode");
    mode.textContent = `⦿ ${s.mode}`;
    mode.className = `chip mode-${s.mode}`;
    const gpu = s.gpu?.[0];
    $("#chip-gpu").textContent = gpu ? `${gpu.model.slice(0, 22)} · ${gpu.total}` : "cpu only";
    $("#chip-toks").textContent = s.busy ? `${(s.tok_s || 0).toFixed(1)} tok/s` : "idle";
    $("#save-flag").textContent = s.saved ? "saved" : "";
    $("#g-vram").textContent = gpu ? `${gpu.free ?? "?"} free / ${gpu.total}` : "—";
    $("#g-vram-note").textContent = s.gpu_total_bytes ? gib(s.gpu_total_bytes) + " total" : "";
    const ctx = $("#ctx-fill");
    const frac = Math.min(1, (s.ctx_used || 0) / Math.max(4096, s.ctx_window || 8192));
    ctx.style.width = (frac * 100).toFixed(1) + "%";
    ctx.classList.toggle("hot", frac > 0.85);
    $("#ctx-label").textContent = `~${s.ctx_used || 0} tok / ctx ${s.ctx_window || "?"}`;
    $("#audit-note").textContent = `every gated action is journaled in ~/.ohmyllm/logs/audit.jsonl`;
    if (s.session && s.session !== state.session) state.session = s.session;
    $("#g-title").textContent = s.session_title || "—";
    if (!state.config) state.config = await get("/api/config");
  } catch (e) {
    $("#boot-error").hidden = false; $("#boot-error-msg").textContent = e.message;
  }
}

/* -------------------------------------------------------------- palette */
const CMDS = [
  ["Market", () => switchView("market"), ""], ["New chat", () => newChat(), "Ctrl N"], ["Open chats", () => switchView("chats"), "Ctrl O"],
  ["Models", () => switchView("models"), "Ctrl M"], ["Backend", () => switchView("backend"), "Ctrl B"],
  ["Memory (soul.md)", () => switchView("memory"), "Ctrl S"], ["Doctor", () => switchView("doctor"), "Ctrl D"],
  ["Settings", () => switchView("settings"), "Ctrl G"], ["Help", () => switchView("help"), "Ctrl H"],
  ["Permission mode…", () => setModeUi(), "Ctrl P"], ["Stop current turn", () => post("/api/cancel", {})],
  ["Re-run backend probe", () => { renderBackends(); refreshStatus(); }],
  ["Remember something…", () => { $("#input").value = "/remember "; $("#composer").requestSubmit?.() ?? switchView("chat"); $("#input").focus(); }],
];
let palSel = 0;
function openPalette() {
  $("#palette-input").value = ""; palSel = 0;
  renderPalette("");
  openModal("#palette-modal"); $("#palette-input").focus();
}
function renderPalette(q) {
  const hits = CMDS.filter((c) => c[0].toLowerCase().includes(q.toLowerCase()));
  $("#palette-list").innerHTML = hits.map((c, i) => `<li class="${i === palSel ? "sel" : ""}" data-i="${i}">${c[0]}<small>${c[2] || ""}</small></li>`).join("");
  $$("#palette-list li").forEach((li) => (li.onclick = () => { hits[+li.dataset.i][1](); closeModal("#palette-modal"); $("#scrim").hidden = true; }));
  return hits;
}
$("#open-palette").onclick = openPalette;
$("#palette-input").oninput = () => { palSel = 0; renderPalette($("#palette-input").value); };
$("#palette-input").onkeydown = (e) => {
  const hits = renderPalette($("#palette-input").value);
  if (e.key === "ArrowDown") { palSel = Math.min(palSel + 1, hits.length - 1); renderPalette($("#palette-input").value); }
  if (e.key === "ArrowUp") { palSel = Math.max(palSel - 1, 0); renderPalette($("#palette-input").value); }
  if (e.key === "Enter" && hits[palSel]) { hits[palSel][1](); closeModal("#palette-modal"); $("#scrim").hidden = true; }
  if (e.key === "Escape") { closeModal("#palette-modal"); $("#scrim").hidden = true; }
};

/* ------------------------------------------------------------- wizard */
const WZ = [
  { t: "Welcome to OhMyLLM", b: `<p>Your own agent runtime on your own machine. Models stay local, tools are permission-gated, and memory lives in a plain <code>soul.md</code> you own.</p><p class="muted small">This setup runs once; you can redo everything later from the sidebar.</p>` },
  { t: "Your hardware", b: `<div id="wz-hw" class="muted">detecting…</div>` },
  { t: "Pick a backend", b: `<div id="wz-be"></div>` },
  { t: "Pick a model", b: `<div id="wz-md">choose a backend first</div>` },
  { t: "Permission mode", b: `<div id="wz-mode"></div>` },
  { t: "Who are you?", b: `<p>The agent's memory starts empty. Tell it a little — these go into <b>soul.md</b>.</p>
    <input type="text" id="wz-name" placeholder="Your name" style="width:100%;margin-bottom:8px;background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:9px 12px;color:var(--fg)">
    <input type="text" id="wz-ctx" placeholder="What you're working on" style="width:100%;background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:9px 12px;color:var(--fg)">` },
  { t: "Ready", b: `<p>Seeded. A few things to try:</p>
    <div class="opt"><div><b>“list files in . and summarize”</b><div class="d">a gated tool run with an approval card</div></div></div>
    <div class="opt"><div><b>“remember: my rabbit is called Bnuy”</b><div class="d">persistent memory — check the Memory screen after</div></div></div>
    <div class="opt"><div><b>press <kbd>Ctrl K</kbd></b><div class="d">everything is one keystroke away</div></div></div>` },
];
let wzIdx = 0;
function wzShow() {
  $("#wz-steps").innerHTML = WZ.map((_, i) => `<i class="${i < wzIdx ? "done" : i === wzIdx ? "cur" : ""}"></i>`).join("");
  $("#wz-body").innerHTML = `<h2>${WZ[wzIdx].t}</h2>` + WZ[wzIdx].b;
  $("#wz-back").disabled = wzIdx === 0;
  $("#wz-next").textContent = wzIdx === WZ.length - 1 ? "Start chatting" : "Next";
  if (wzIdx === 1) wzHw();
  if (wzIdx === 2) wzBe();
  if (wzIdx === 3) wzModels();
  if (wzIdx === 4) wzMode();
}
async function wzHw() {
  const el = $("#wz-hw");
  const s = state.status || (await (await fetch(`${BASE}/api/status`, { headers: { "X-OMLLM-Token": BOOT.token } })).json());
  const gpus = s.gpu || [];
  el.innerHTML = gpus.length
    ? gpus.map((g) => `<div class="be-row"><span class="tagpill">${esc(g.model)}</span> ${g.total}${g.free ? ` · ${g.free} free` : ""}</div>`).join("")
    : `<div class="be-row">No GPU detected — CPU inference works, small quantized models recommended.</div>`;
  el.insertAdjacentHTML("beforeend", `<p class="muted small" style="margin-top:10px">backend: ${esc(s.backend)} · mode: ${esc(s.mode)}</p>`);
}
async function wzBe() {
  const el = $("#wz-be"); if (!el) return;
  const rows = await get("/api/backends");
  el.innerHTML = "";
  [["ollama", "Ollama — tool-calling llama.cpp server"], ["lmstudio", "LM Studio — desktop app API"], ["cloud", "☁ Cloud — hosted providers via API key"]]
    .forEach(([k, t]) => {
      const o = document.createElement("div");
      o.className = "opt" + (state.config?.default_backend === k ? " sel" : "");
      const st = rows[k]?.running ? '<span class="fit-ok">● running</span>' : rows[k]?.installed ? '<span class="fit-tight">○ installed</span>' : '<span class="muted">– not found</span>';
      o.innerHTML = `<div><span class="t">${t.split(" — ")[0]}</span> ${st}<div class="d">${t.split(" — ")[1]}</div></div>`;
      o.onclick = async () => { el.querySelectorAll(".opt").forEach((x) => x.classList.remove("sel")); o.classList.add("sel"); await post("/api/backend", { name: k }); state.config = await get("/api/config"); el.dataset.pick = k; refreshStatus(); };
      el.appendChild(o);
    });
}
async function wzModels() {
  const el = $("#wz-md"); if (!el) return;
  el.innerHTML = `<p class="muted small">loading…</p>`;
  const be = state.config?.default_backend || "mock";
  const d = await get(`/api/models?backend=${be}`);
  if (d.error) { el.innerHTML = `<p class="fit-tight">backend not reachable: ${esc(d.error)}</p><p class="muted small">Pick <b>Backend → start</b> later, or use the mock backend — it works offline.</p>`; return; }
  el.innerHTML = "";
  (d.models || []).forEach((m) => {
    const o = document.createElement("div");
    o.className = "opt" + (m.name === d.default ? " sel" : "");
    o.innerHTML = `<div><span class="t">${esc(m.name)}</span><div class="d">${m.size} · ${m.fit} fit${m.tool_capable ? " · tools ok" : " · tools weak"}</div></div>`;
    o.onclick = async () => { el.querySelectorAll(".opt").forEach((x) => x.classList.remove("sel")); o.classList.add("sel"); await post("/api/model", { backend: be, model: m.name }); refreshStatus(); };
    el.appendChild(o);
  });
  if (!d.models?.length) el.innerHTML = `<p class="muted">No models pulled yet — that's fine, continue and pull one later from <b>Models → Pull</b>.</p>`;
}
function wzMode() {
  const el = $("#wz-mode"); if (!el) return;
  el.innerHTML = "";
  [["safe", "Safe — recommended default"], ["strict", "Strict — approve everything that changes things"], ["sandboxed", "Sandboxed — allowlisted commands, workspace-scoped writes"]].forEach(([m, t]) => {
    const o = document.createElement("div");
    o.className = "opt" + (state.status?.mode === m ? " sel" : "");
    o.innerHTML = `<div><span class="t">${t.split(" — ")[0]}</span><div class="d">${t.split(" — ")[1]}</div></div>`;
    o.onclick = async () => { el.querySelectorAll(".opt").forEach((x) => x.classList.remove("sel")); o.classList.add("sel"); await post("/api/mode", { mode: m }); refreshStatus(); };
    el.appendChild(o);
  });
  el.insertAdjacentHTML("beforeend", `<p class="muted small">dangerous & full_access exist but must be chosen later, deliberately.</p>`);
}
async function wzSeed() {
  const name = $("#wz-name")?.value.trim(), ctx = $("#wz-ctx")?.value.trim();
  if (name) await post("/api/soul", { action: "upsert", section: "Identity", text: `user's name is ${name}` });
  if (ctx) await post("/api/soul", { action: "upsert", section: "Current Work", text: ctx });
}
$("#wz-next").onclick = async () => {
  if (wzIdx === 5) await wzSeed();
  wzIdx++;
  if (wzIdx >= WZ.length) { $("#wizard").hidden = true; localStorage.setItem("omllm_setup_done", "1"); await patch("/api/config", { first_run_completed: true }); toast("welcome aboard"); refreshStatus(); return; }
  wzShow();
};
$("#wz-back").onclick = () => { if (wzIdx) { wzIdx--; wzShow(); } };

/* --------------------------------------------------------------- global keys */
const KEYMAP = { m: "models", b: "backend", p: () => setModeUi(), s: "memory", d: "doctor", g: "settings", o: "chats", h: "help", n: () => newChat(), k: openPalette };

/* ------------------------------------------------- hold-to-talk (Ctrl+E)
 * The audio is captured SERVER-side (pw-record/arecord, the same recorder
 * the TUI uses) — the Tauri webview has no getUserMedia permission handler
 * and browsers deny it on a whim, but the bridge always sits next to the
 * real microphone. Ctrl+E down → start; up → stop + transcribe. */
let _mic = false, _micAuto = null, _micBusy = false;
const _inputPlaceholder = $("#input").placeholder;
function talkUi(on) {
  const inp = $("#input");
  inp.classList.toggle("mic-live", on);
  inp.placeholder = on ? "🎙 listening… release Ctrl+E when done" : _inputPlaceholder;
}
async function talkStart() {
  if (_mic || _micBusy || state.streaming) return;
  _mic = true;
  talkUi(true);
  let r;
  try { r = await post("/api/voice/record", { action: "start" }); }
  catch (err) { r = { error: String(err.message || err) }; }
  if (r && r.error) {
    _mic = false;
    talkUi(false);
    toast(r.error, "bad");
    return;
  }
  _micAuto = setTimeout(talkStop, 30000);  // never leave the mic hot
}
async function talkStop() {
  if (!_mic) return;
  _mic = false;
  clearTimeout(_micAuto);
  talkUi(false);
  if (_micBusy) return;
  _micBusy = true;
  toast("🎧 transcribing…");
  let r;
  try { r = await post("/api/voice/record", { action: "stop" }); }
  catch (err) { r = { error: String(err.message || err) }; }
  _micBusy = false;
  if (r && r.error) { toast(r.error, "bad"); return; }
  if (r && r.text) {
    const inp = $("#input");
    inp.value = (inp.value + " " + r.text).trim();
    inp.focus();
    toast(`🎙 ${r.text.slice(0, 60)}${r.text.length > 60 ? "…" : ""}`);
  }
}
document.addEventListener("keydown", async (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey) {
    const k = e.key.toLowerCase();
    if (k === "q") { e.preventDefault(); if (await confirmModal("quit", "Quit the OhMyLLM window?", "Quit")) window.close(); return; }
    if (k === "e" && e.ctrlKey && !e.metaKey && !e.repeat) { e.preventDefault(); talkStart(); return; }
    const fn = KEYMAP[k];
    if (fn) { e.preventDefault(); typeof fn === "function" ? fn() : switchView(fn); }
    if (k === "enter" && $("#view-memory")?.hidden === false) { e.preventDefault(); $("#soul-save").click(); }
  }
  if (e.key === "Escape" && !$("#approval-modal").hidden && state.approvalQueue.length) answerApproval("deny");
  if (!$("#approval-modal").hidden) {
    const k = e.key.toLowerCase();
    if (k === "y") answerApproval("approve");
    if (k === "n") answerApproval("deny");
    if (k === "a") answerApproval("session");
    if (k === "p") answerApproval("always");
  }
  if (e.key.toLowerCase() === "e" && !e.ctrlKey && !e.metaKey && !e.altKey && _mic) talkStop();
});
document.addEventListener("keyup", (e) => { if (e.code === "KeyE") talkStop(); });
window.addEventListener("blur", () => talkStop());

/* ------------------------------------------------------------------ boot */
async function boot() {
  await refreshStatus();
  if (!localStorage.getItem("omllm_setup_done") && BOOT.first_run === false) { $("#wizard").hidden = false; wzShow(); }
  setInterval(refreshStatus, 5000);
  if (state.status?.session) loadSessionIntoView(state.status.session);
  else emptyState();
  $("#input").focus();
}
boot();
