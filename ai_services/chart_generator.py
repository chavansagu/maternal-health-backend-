"""
Chart Generator - Auto-detect chart type and generate config
"""
from typing import Dict, List, Any, Optional

class ChartGenerator:
    """Generates chart configuration from SQL results"""
    
    def generate_chart_config(self, data: List[Dict], sql: str, question: str) -> Optional[Dict]:
        """
        Auto-detect chart type and generate config
        
        Returns:
            Chart config dict or None if data is not suitable for charts
        """
        if not data or len(data) == 0:
            return None
        
        # Single value result (COUNT, SUM, AVG)
        if len(data) == 1 and len(data[0]) == 1:
            return self._generate_metric_card(data, question)
        
        # Multiple rows with 2 columns (category + value)
        if len(data[0]) == 2:
            # Validate that we have one text column and one numeric column
            keys = list(data[0].keys())
            first_val = data[0][keys[0]]
            second_val = data[0][keys[1]]
            
            # Check if columns are in correct order (text, number)
            first_is_numeric = isinstance(first_val, (int, float))
            second_is_numeric = isinstance(second_val, (int, float))
            
            # If both are numeric or both are text, use table view
            if (first_is_numeric and second_is_numeric) or (not first_is_numeric and not second_is_numeric):
                return self._generate_table_config(data, question)
            
            return self._generate_category_chart(data, question, sql)
        
        # Multiple columns - table view
        if len(data[0]) > 2:
            return self._generate_table_config(data, question)
        
        # Default: table
        return self._generate_table_config(data, question)
    
    def _generate_metric_card(self, data: List[Dict], question: str) -> Dict:
        """Single metric display"""
        key = list(data[0].keys())[0]
        value = data[0][key]
        
        return {
            "type": "metric",
            "config": {
                "title": question,
                "value": value,
                "label": key.replace('_', ' ').title()
            }
        }
    
    def _generate_category_chart(self, data: List[Dict], question: str, sql: str) -> Dict:
        """Bar/Pie/Line chart for category + value"""
        keys = list(data[0].keys())
        category_field = keys[0]
        value_field = keys[1]
        
        # Extract data with safe type conversion
        labels = [str(row[category_field]) for row in data]
        values = []
        for row in data:
            val = row[value_field]
            try:
                # Try to convert to float
                values.append(float(val) if val is not None else 0)
            except (ValueError, TypeError):
                # If conversion fails, it might be a string in wrong column
                # Skip this row or use 0
                values.append(0)
        
        # Determine chart type based on question keywords
        chart_type = "bar"  # Default to bar

        question_lower = question.lower()

        # Line chart for trends/time series
        if any(word in question_lower for word in ["trend", "over time", "monthly", "yearly", "month", "year"]):
            chart_type = "line"
        # Pie chart for distribution/percentage/ratio — only when few categories
        elif any(word in question_lower for word in ["distribution", "percentage", "proportion", "share", "ratio", "gender"]) and len(data) <= 8:
            chart_type = "pie"
        # Bar chart for comparisons, counts, per-entity breakdowns
        else:
            chart_type = "bar"
        
        return {
            "type": chart_type,
            "config": {
                "title": question,
                "xAxis": {
                    "field": category_field,
                    "label": category_field.replace('_', ' ').title()
                },
                "yAxis": {
                    "field": value_field,
                    "label": value_field.replace('_', ' ').title()
                },
                "chartData": data,
                "labels": labels,
                "values": values
            }
        }
    
    def _generate_table_config(self, data: List[Dict], question: str) -> Dict:
        """Table view for complex data"""
        columns = [
            {
                "field": key,
                "label": key.replace('_', ' ').title()
            }
            for key in data[0].keys()
        ]
        
        return {
            "type": "table",
            "config": {
                "title": question,
                "columns": columns,
                "data": data
            }
        }

# Global instance
chart_generator = ChartGenerator()
