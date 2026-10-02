import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

interface DataPreviewTableProps {
  data: string
}

export function DataPreviewTable({ data }: DataPreviewTableProps) {
  // Parse the data preview string into rows
  const lines = data.trim().split('\n')
  if (lines.length < 1) return null

  // Find where the actual table starts (skip "Last X rows:" etc)
  const tableStartIndex = lines.findIndex(l => {
    const trimmed = l.trim()
    return trimmed && !trimmed.includes('DataFrame shape:') && !trimmed.includes('rows:')
  })

  if (tableStartIndex === -1) {
    return (
      <Card className="mt-3">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Data Preview</CardTitle>
        </CardHeader>
        <CardContent>
          <pre className="text-xs text-muted-foreground whitespace-pre-wrap font-mono">{data}</pre>
        </CardContent>
      </Card>
    )
  }

  const headerLine = lines[tableStartIndex]
  let headers = headerLine.split(/\s{2,}|\s+|\t/).filter(Boolean)
  
  const dataLines = lines.slice(tableStartIndex + 1).filter(l => l.trim())
  const rows = dataLines.map(line => {
    return line.split(/\s{2,}|\s+|\t/).filter(Boolean)
  })

  // FIX: If rows have more columns than headers (common with Pandas index), add an "idx" header
  if (rows.length > 0 && rows[0].length > headers.length) {
    headers = ["#", ...headers]
  }

  if (headers.length === 0 || rows.length === 0) {
    return (
      <Card className="mt-3">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Data Preview</CardTitle>
        </CardHeader>
        <CardContent>
          <pre className="text-xs text-muted-foreground whitespace-pre-wrap font-mono">{data}</pre>
        </CardContent>
      </Card>
    )
  }


  return (
    <Card className="mt-3">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Data Preview</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr>
                {headers.map((header, i) => (
                  <th
                    key={i}
                    className="border border-border/50 bg-muted px-2 py-1 text-left font-medium text-foreground"
                  >
                    {header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, rowIdx) => (
                <tr key={rowIdx}>
                  {row.map((cell, cellIdx) => (
                    <td
                      key={cellIdx}
                      className="border border-border/50 px-2 py-1 text-foreground"
                    >
                      {(() => {
                        const header = headers[cellIdx]?.toLowerCase()
                        if ((header === 'time' || header === 'date') && !isNaN(Number(cell)) && Number(cell) > 1000000000) {
                          try {
                            return new Date(Number(cell) * 1000).toLocaleString('en-GB', {
                              day: '2-digit',
                              month: '2-digit',
                              year: 'numeric',
                              hour: '2-digit',
                              minute: '2-digit',
                              hour12: false
                            })
                          } catch {
                            return cell
                          }
                        }
                        return cell
                      })()}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  )
}
